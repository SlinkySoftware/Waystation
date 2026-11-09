"""Process-wide services of one server process, created at start-up (app lifespan) and shared by every request.

``Services.create(settings)`` connects nothing yet; ``start()`` checks the database schema (the server refuses to
run against an unknown newer or not yet migrated schema, SPECIFICATION §8.6) and starts background tasks;
``stop()`` flushes and closes everything. Tests build one against throwaway MongoDB/Valkey and call ``start()``
themselves.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from redis.asyncio import Redis
from starlette.requests import Request

from waystation_common.audit import AuditWriter
from waystation_common.crypto import Keyring
from waystation_common.db import audit as audit_db
from waystation_common.db.blobs import MongoBlobGuard
from waystation_common.db.client import Client, Database, create_client, database
from waystation_common.db.migrations import check_schema
from waystation_common.events import EventEmitter
from waystation_common.jobs import JobClient
from waystation_common.settings import Settings
from waystation_common.storage import BlobStore
from waystation_common.valkey import create_valkey
from waystation_server.db import auth_providers as auth_providers_db
from waystation_server.db import groups as groups_db
from waystation_server.db import repositories as repos_db
from waystation_server.formats.registry import FormatRegistry
from waystation_server.metrics import Metrics
from waystation_server.proxy.engine import ProxyEngine
from waystation_server.proxy.http import UpstreamClients
from waystation_server.repos.catalog import RepoCatalog
from waystation_server.repos.model import RepoConfig
from waystation_server.security.groups import GroupDirectory
from waystation_server.security.ldap import LdapAuthenticator
from waystation_server.security.oidc import OidcClient, UpstreamHttp
from waystation_server.security.principal import Principal
from waystation_server.security.providers import ProviderCatalog
from waystation_server.security.registry_token import SignerHolder
from waystation_server.security.sessions import SessionManager
from waystation_server.security.tokens import TokenService
from waystation_server.sse.hub import Hub
from waystation_server.stats.storage import StorageStats

log = logging.getLogger('waystation.server')

BearerAuthenticator = Callable[[Request, str], Awaitable[Principal | None]]


@dataclass
class Services:
    settings: Settings
    db: Database
    valkey: Redis
    emitter: EventEmitter
    blobs: BlobStore
    catalog: RepoCatalog
    formats: FormatRegistry
    metrics: Metrics
    sessions: SessionManager
    tokens: TokenService
    upstream: UpstreamClients
    proxy: ProxyEngine
    storage: StorageStats
    hub: Hub
    registry_signer: SignerHolder
    auth_providers: ProviderCatalog  # external sign-in providers (security.providers)
    groups: GroupDirectory  # group → roles (security.groups)
    ldap: LdapAuthenticator  # LDAP thread pool front end + bind cache (security.ldap)
    oidc: OidcClient  # OIDC discovery/JWKS caches and protocol steps (security.oidc)
    jobs: JobClient  # enqueue worker jobs (durable via the outbox inside a transaction, or best effort; ARCH §10)
    audit: AuditWriter  # audit records, queued and batched (``waystation_server.audit.record``)
    # Tried in order for ``Authorization: Bearer`` when there is no session (the Docker token service adds one).
    bearer_authenticators: list[BearerAuthenticator] = field(default_factory=list)
    keyring: Keyring | None = None
    mongo_client: Client | None = None
    check_schema_on_start: bool = True
    _started: bool = field(default=False, repr=False)

    @classmethod
    def create(
        cls,
        settings: Settings,
        *,
        formats: FormatRegistry | None = None,
        mongo_client: Client | None = None,
        valkey: Redis | None = None,
        check_schema_on_start: bool = True,
    ) -> Services:
        client = mongo_client or create_client(settings, app_name='waystation-server')
        db = database(client, settings)
        redis = valkey or create_valkey(settings)
        emitter = EventEmitter(redis)
        metrics = Metrics(inflight=lambda: float(emitter.live_transfers))
        if formats is None:
            from waystation_server.plugins import default_registry

            formats = default_registry()
        keyring = Keyring.from_settings(settings.secret_keys) if settings.secret_keys else None

        async def load() -> list[RepoConfig]:
            return await repos_db.list_all(db)

        blobs = BlobStore.from_settings(settings)
        blobs.on_refused = metrics.storage_refused
        blobs.guard = MongoBlobGuard(db)  # commits claim their blob against GC (waystation_common.db.blobs)
        storage = StorageStats(db, settings.data_dir)
        catalog = RepoCatalog(load)
        groups = GroupDirectory(lambda: groups_db.list_all(db))

        async def visible(principal: Principal) -> set[str]:
            from waystation_server.security import access

            return {r.name for r in await catalog.all() if access.can_see(principal, r)}

        async def figures() -> tuple[int, int]:
            current = await storage.get()
            return current.used_bytes, current.artifacts

        upstream = UpstreamClients(settings)
        hub = Hub(redis, visible, figures)
        metrics.track(metrics.sse_clients, lambda: float(len(hub.subscribers)))
        return cls(
            settings=settings,
            db=db,
            valkey=redis,
            emitter=emitter,
            blobs=blobs,
            catalog=catalog,
            formats=formats,
            metrics=metrics,
            sessions=SessionManager(db, redis, settings),
            tokens=TokenService(db, redis, settings, roles_for=groups.roles_for),
            upstream=upstream,
            storage=storage,
            hub=hub,
            registry_signer=SignerHolder(settings.data_dir, keyring),
            auth_providers=ProviderCatalog(lambda: auth_providers_db.list_all(db)),
            groups=groups,
            ldap=LdapAuthenticator(),
            oidc=OidcClient(UpstreamHttp(upstream)),
            jobs=JobClient(redis, db),
            audit=AuditWriter(audit_db.inserter(db), retention_days=settings.audit_retention_days),
            proxy=ProxyEngine(
                settings=settings,
                db=db,
                valkey=redis,
                blobs=blobs,
                emitter=emitter,
                clients=upstream,
                metrics=metrics,
                keyring=keyring,
            ),
            keyring=keyring,
            mongo_client=client,
            check_schema_on_start=check_schema_on_start,
        )

    @property
    def base_url(self) -> str:
        return self.settings.base_url

    async def start(self) -> None:
        if self._started:
            return
        if self.check_schema_on_start:
            await check_schema(self.db)
        if self.keyring is None:
            log.warning('WAYSTATION_SECRET_KEYS is not set: upstream credentials cannot be stored')
        await self.emitter.start()
        self.audit.start()
        await self.proxy.coalescer.start()
        self.tokens.start()
        self.metrics.start()
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        await self.metrics.stop()
        await self.hub.stop()
        await self.proxy.coalescer.aclose()
        await self.tokens.stop()
        await self.audit.aclose()
        await self.emitter.aclose()
        await self.upstream.aclose()
        await self.valkey.aclose()
        if self.mongo_client is not None:
            await self.mongo_client.close()
