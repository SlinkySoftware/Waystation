"""API tokens (SPECIFICATION §4.9, ARCHITECTURE §7, DATA_MODEL ``api_tokens``).

Secrets
-------
A token is ``wst_`` + 32 random bytes (base64url, 43 characters). It is shown once, at creation; MongoDB stores only
``hash`` = HMAC-SHA256 of the token under a key derived from each ``WAYSTATION_SECRET_KEYS`` entry (a fast keyed hash:
the token is high-entropy, so a slow password hash buys nothing, and the key means a database dump alone cannot
confirm a guessed token). The first key hashes new tokens; lookups try every configured key (``hash $in [...]``, index
``hash_unique``), so rotating the secret keys does not invalidate tokens. ``prefix`` (``wst_`` + 8 characters) is
kept for display. Without secret keys the hash is unkeyed SHA-256 and the server warns.

Authentication and revocation
-----------------------------
``TokenService.authenticate(secret)`` resolves a token to its owner (a user or a service account) narrowed to the
token's scopes and repository patterns (``Principal.token``; decisions in ``security.access``). The resolved
principal is cached per process for at most ``CACHE_S`` (the owner's roles and status are re-read after that).
Revoking a token, or changing a service account, publishes on the Valkey channel ``revocations`` (a token id, or
``*`` for everything); every replica's listener drops the cached entry at once, so a revoked token is refused
everywhere well within a second. When the listener reconnects it clears the whole cache. MongoDB stays authoritative.

Last used
---------
Use is recorded in Valkey (``seen:tokens``: token id → ``<epoch seconds> <client address>``), at most once a minute
per token and process, never in MongoDB on the request path (ARCHITECTURE §8); the worker's rollup flushes it.

Failed attempts are counted per client address (``rl:token:<hash>``); past ``WAYSTATION_TOKEN_FAILURE_LIMIT`` per
5 minutes the answer is 429.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis

from waystation_common.crypto import parse_key
from waystation_common.db import seen
from waystation_common.db.client import Database
from waystation_common.errors import Unauthorized
from waystation_common.ids import new_id
from waystation_common.settings import Settings
from waystation_server import audit
from waystation_server.audit import AuditAction, Outcome
from waystation_server.db import service_accounts as accounts_db
from waystation_server.db import tokens as tokens_db
from waystation_server.db import users as users_db
from waystation_server.security import ratelimit
from waystation_server.security.identity import ApiTokenRecord, TokenOwner, UserRecord
from waystation_server.security.principal import AuthMethod, Principal, PrincipalKind, TokenLimits

log = logging.getLogger('waystation.tokens')

TOKEN_PREFIX = 'wst_'  # noqa: S105 - the public prefix, not a secret
_TOKEN_RE = re.compile(r'^wst_[A-Za-z0-9_-]{43}$')
PREFIX_LEN = len(TOKEN_PREFIX) + 8
HASH_INFO = b'waystation api token hash v1'
REVOCATIONS = 'revocations'
REVOKE_ALL = '*'
CACHE_S = 5.0
TOUCH_EVERY_S = 60.0
FAILURE_WINDOW_S = 300
MAX_CACHE = 50_000
INVALID = 'invalid, expired or revoked token'


def generate() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def looks_like_token(value: str) -> bool:
    return value.startswith(TOKEN_PREFIX)


def display_prefix(token: str) -> str:
    return token[:PREFIX_LEN]


class TokenHasher:
    """HMAC-SHA256 under keys derived from the secret keys (first = current)."""

    def __init__(self, keys: list[bytes]) -> None:
        self._keys = [hmac.new(key, HASH_INFO, hashlib.sha256).digest() for key in keys] or [b'']
        self.keyed = bool(keys)

    @classmethod
    def from_settings(cls, settings: Settings) -> TokenHasher:
        return cls([parse_key(value.get_secret_value()) for value in settings.secret_keys])

    def hash(self, token: str) -> str:
        return self.candidates(token)[0]

    def candidates(self, token: str) -> list[str]:
        data = token.encode('utf-8')
        if not self.keyed:
            return [hashlib.sha256(data).hexdigest()]
        return [hmac.new(key, data, hashlib.sha256).hexdigest() for key in self._keys]


@dataclass(frozen=True)
class _Entry:
    loaded_at: float
    principal: Principal | None
    expires_at: float | None  # epoch seconds


def token_limits(record: ApiTokenRecord) -> TokenLimits:
    from waystation_server.security.access import token_actions

    return TokenLimits(
        token_id=record.id,
        actions=token_actions(record.scopes),
        repositories=tuple(record.repo_globs),
        scopes=tuple(record.scopes),
    )


class TokenService:
    def __init__(
        self,
        db: Database,
        valkey: Redis,
        settings: Settings,
        *,
        roles_for: Callable[[UserRecord], Awaitable[tuple[str, ...]]] | None = None,
    ) -> None:
        self._roles_for = roles_for  # effective roles of a token owner (own + groups, security.groups)
        self._db = db
        self._valkey = valkey
        self._settings = settings
        self.hasher = TokenHasher.from_settings(settings)
        self._by_id: dict[str, _Entry] = {}
        self._by_hash: dict[str, tuple[float, str | None]] = {}  # hash -> (loaded_at, token id or None = unknown)
        self._touched: dict[str, float] = {}
        self._listener: asyncio.Task[None] | None = None
        self.subscribed = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.hasher.keyed:
            log.warning('WAYSTATION_SECRET_KEYS is not set: API tokens are hashed without a key')
        if self._listener is None:
            self._listener = asyncio.create_task(self._listen(), name='token-revocations')

    async def stop(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener
            self._listener = None

    async def _listen(self) -> None:
        while True:
            pubsub = self._valkey.pubsub()
            try:
                await pubsub.subscribe(REVOCATIONS)
                self.clear()  # anything published while we were not listening is unknown
                self.subscribed.set()
                async for message in pubsub.listen():
                    if message.get('type') != 'message':
                        continue
                    data = message.get('data')
                    self.invalidate(data.decode() if isinstance(data, bytes) else str(data))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.subscribed.clear()
                log.warning('revocation channel lost; reconnecting', extra={'error': type(exc).__name__})
                self.clear()
                await asyncio.sleep(1.0)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()  # type: ignore[no-untyped-call]

    # ------------------------------------------------------------------ cache
    def clear(self) -> None:
        self._by_id.clear()
        self._by_hash.clear()

    def invalidate(self, token_id: str) -> None:
        if token_id == REVOKE_ALL:
            self.clear()
            return
        self._by_id.pop(token_id, None)

    async def announce(self, token_id: str) -> None:
        """Tell every replica (including this one) to drop ``token_id`` (or everything: ``*``)."""
        self.invalidate(token_id)
        try:
            await self._valkey.publish(REVOCATIONS, token_id)
        except Exception as exc:  # replicas fall back to the cache lifetime (ARCHITECTURE §7)
            log.warning('revocation announcement failed', extra={'error': type(exc).__name__})

    # ------------------------------------------------------------------ resolution
    async def _owner_principal(self, record: ApiTokenRecord) -> Principal | None:
        limits = token_limits(record)
        if record.principal.kind == 'user':
            user = await users_db.get(self._db, record.principal.id)
            if user is None or user.disabled:
                return None
            roles = await self._roles_for(user) if self._roles_for is not None else tuple(user.roles)
            return Principal(
                PrincipalKind.USER,
                user.id,
                user.username,
                roles,
                AuthMethod.API_TOKEN,
                token=limits,
                provider=user.source,
            )
        account = await accounts_db.get(self._db, record.principal.id)
        if account is None or account.disabled:
            return None
        return Principal(
            PrincipalKind.SERVICE_ACCOUNT,
            account.id,
            account.name,
            tuple(account.roles),
            AuthMethod.API_TOKEN,
            token=limits,
        )

    async def _load(self, record: ApiTokenRecord | None, now: float) -> _Entry:
        if record is None or record.revoked_at is not None:
            return _Entry(now, None, None)
        principal = await self._owner_principal(record)
        expires = record.expires_at.timestamp() if record.expires_at else None
        return _Entry(now, principal, expires)

    def _remember(self, token_id: str, entry: _Entry) -> None:
        if len(self._by_id) >= MAX_CACHE:
            self._by_id.clear()
        self._by_id[token_id] = entry

    async def by_id(self, token_id: str) -> Principal | None:
        """The live principal of token ``token_id`` (registry tokens issued for an API token re-check it)."""
        now = time.monotonic()
        entry = self._by_id.get(token_id)
        if entry is None or now - entry.loaded_at >= CACHE_S:
            entry = await self._load(await tokens_db.get(self._db, token_id), now)
            self._remember(token_id, entry)
        return self._live(entry)

    @staticmethod
    def _live(entry: _Entry) -> Principal | None:
        if entry.principal is None:
            return None
        if entry.expires_at is not None and entry.expires_at <= time.time():
            return None
        return entry.principal

    async def resolve(self, secret: str) -> Principal | None:
        """The principal of a presented token, or None (unknown, revoked, expired, owner disabled)."""
        if not _TOKEN_RE.fullmatch(secret):
            return None
        hashes = self.hasher.candidates(secret)
        now = time.monotonic()
        known = self._by_hash.get(hashes[0])
        if known is not None and now - known[0] < CACHE_S:
            if known[1] is None:
                return None
            entry = self._by_id.get(known[1])
            if entry is not None and now - entry.loaded_at < CACHE_S:
                return self._live(entry)
        record = await tokens_db.find_by_hashes(self._db, hashes)
        if len(self._by_hash) >= MAX_CACHE:
            self._by_hash.clear()
        self._by_hash[hashes[0]] = (now, record.id if record else None)
        if record is None:
            return None
        entry = await self._load(record, now)
        self._remember(record.id, entry)
        return self._live(entry)

    async def authenticate(self, secret: str, *, client_ip: str) -> Principal:
        """Resolve a presented token or raise ``Unauthorized`` (``RateLimited`` after too many failures)."""
        principal = await self.resolve(secret)
        if principal is None:
            log.info('token refused', extra={'client_ip': client_ip})
            await ratelimit.check(
                self._valkey,
                'token',
                client_ip,
                limit=self._settings.token_failure_limit,
                window_s=FAILURE_WINDOW_S,
            )
            audit.record(
                AuditAction.TOKEN_USE_REFUSED,
                outcome=Outcome.FAILURE,
                reason='invalid_token',
                target=audit.target('api_token', None, display_prefix(secret) if _TOKEN_RE.match(secret) else None),
                details={'prefix': display_prefix(secret) if _TOKEN_RE.match(secret) else None},
            )
            raise Unauthorized(INVALID)
        assert principal.token is not None
        await self._touch(principal.token.token_id, client_ip)
        return principal

    # ------------------------------------------------------------------ lifecycle of a token
    async def create(
        self,
        owner: TokenOwner,
        *,
        name: str,
        scopes: list[str],
        repo_globs: list[str],
        expires_at: datetime | None,
        created_by: str | None,
    ) -> tuple[ApiTokenRecord, str]:
        """Store a new token; returns the record and the secret (shown once, never stored)."""
        secret = generate()
        record = ApiTokenRecord(
            id=new_id(),
            principal=owner,
            name=name,
            prefix=display_prefix(secret),
            hash=self.hasher.hash(secret),
            scopes=scopes,
            repo_globs=repo_globs,
            expires_at=expires_at,
            created_at=datetime.now(UTC),
            created_by=created_by,
        )
        await tokens_db.insert(self._db, record)
        log.info('api token created', extra={'token_id': record.id, 'owner_kind': owner.kind, 'owner_id': owner.id})
        return record, secret

    async def revoke(self, token_id: str, *, by: str | None) -> bool:
        revoked = await tokens_db.revoke(self._db, token_id, now=datetime.now(UTC), by=by)
        await self.announce(token_id)
        if revoked:
            log.info('api token revoked', extra={'token_id': token_id})
        return revoked

    async def revoke_all(self, owner: TokenOwner, *, by: str | None) -> int:
        ids = await tokens_db.revoke_all_for(self._db, owner, now=datetime.now(UTC), by=by)
        for token_id in ids:
            await self.announce(token_id)
        return len(ids)

    # ------------------------------------------------------------------ last used
    async def _touch(self, token_id: str, client_ip: str) -> None:
        now = time.time()
        if now - self._touched.get(token_id, 0.0) < TOUCH_EVERY_S:
            return
        if len(self._touched) > 100_000:
            self._touched.clear()
        self._touched[token_id] = now
        try:
            await self._valkey.hset(seen.SEEN_TOKENS, token_id, f'{int(now)} {client_ip[:64]}')
        except Exception as exc:
            log.warning('token last-used update failed', extra={'error': type(exc).__name__})

    async def pending_use(self, token_ids: list[str]) -> dict[str, tuple[datetime, str | None]]:
        """Last use recorded in Valkey and not yet flushed to MongoDB (for listings)."""
        if not token_ids:
            return {}
        try:
            values = await self._valkey.hmget(seen.SEEN_TOKENS, token_ids)
        except Exception as exc:
            log.warning('token last-used lookup failed', extra={'error': type(exc).__name__})
            return {}
        out: dict[str, tuple[datetime, str | None]] = {}
        for token_id, raw in zip(token_ids, values, strict=True):
            parsed = seen.parse(raw.decode() if isinstance(raw, bytes) else raw) if raw is not None else None
            if parsed is not None:
                out[token_id] = parsed
        return out
