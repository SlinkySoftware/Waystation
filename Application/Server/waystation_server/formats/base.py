"""The format plugin contract (ARCHITECTURE §11).

A format is two things:

1. A **descriptor** (``FormatDescriptor``): name, label, the repository types it supports, the pydantic model of its
   repository options (``options_model``, the ``config_schema`` of ARCH §11, validated on create/update and
   published in OpenAPI), the upstream probe used by "test upstream", the "Set me up" snippets and UI hints. The REST
   API only ever sees descriptors (ARCH §3).
2. Optionally a **plugin** (``FormatPlugin``) that serves the protocol. The server's router resolves the repository,
   normalises the path, authorises the request and then calls ``plugin.handle(req)`` with a ``FormatRequest``:

   - ``/repository/<repo>/<path>`` → ``req.root == 'repository'``, ``req.path`` the normalised path ('' = root).
   - Docker ``/v2/<name>/...`` → ``req.root == 'v2'``, ``req.path`` the part after the repository prefix or alias
     (``library/nginx/manifests/latest``), resolved per SPECIFICATION §4.4 (``waystation_server.routing``).

   The plugin answers with a Starlette ``Response``. Cached content is served by ``req.proxy.serve(req, spec)``
   (``waystation_server.proxy``): fetch, tee, validate, commit, X-Accel; plugins never stream cached bytes
   themselves. Hosted repositories receive, store, serve and delete through ``req.hosted``
   (``waystation_server.hosted.store``).

Repository-level access (``read`` for GET/HEAD and for POST to the descriptor's ``read_posts``) is checked before
``handle`` is called; a plugin that performs other actions (``write`` on PUT...) checks them with
``req.require(Action.WRITE)``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.responses import Response

from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.security.principal import Principal

if TYPE_CHECKING:
    from waystation_server.hosted.store import HostedStore
    from waystation_server.proxy.engine import ProxyEngine
    from waystation_server.security.actions import Action

Root = Literal['repository', 'v2']


# ---------------------------------------------------------------------------- "Set me up" (OpenAPI SetupGuide)
class SetupSnippet(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    title: str
    description: str | None = None
    language: str
    filename: str | None = None
    os: Literal['linux', 'macos', 'windows'] | None = None
    content: str


class SetupClient(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    label: str
    snippets: list[SetupSnippet]


@dataclass(frozen=True)
class SetupContext:
    repo: RepoConfig
    base_url: str  # WAYSTATION_BASE_URL, no trailing slash
    registry_host: str  # host[:port] docker clients use
    username: str | None  # the signed-in user; snippets use {{USERNAME}} / {{TOKEN}} placeholders, never secrets

    @property
    def repo_url(self) -> str:
        return f'{self.base_url}/repository/{self.repo.name}/'


@dataclass(frozen=True)
class UiHints:
    icon: str
    browse: str = 'tree'  # renderer hint: tree, docker, npm, apt


def _no_snippets(ctx: SetupContext) -> list[SetupClient]:
    return []


def _root_probe(options: BaseModel) -> str:
    return ''


def _any_options(options: BaseModel, repo_type: RepoType) -> list[tuple[str, str]]:
    return []


@dataclass(frozen=True)
class FormatDescriptor:
    name: str
    label: str
    types: frozenset[RepoType]
    options_model: type[BaseModel]
    ui: UiHints
    # Path (relative to the upstream URL, no leading slash) that "test upstream" requests; '' = the URL itself.
    probe_path: Callable[[BaseModel], str] = _root_probe
    setup_snippets: Callable[[SetupContext], list[SetupClient]] = _no_snippets
    notes: tuple[str, ...] = ()
    # Repository-relative paths where POST only queries (npm audit): the router authorises them as ``read``.
    read_posts: frozenset[str] = frozenset()
    # Per-type option rules: ``(field, message)`` for options that make no sense for the repository type.
    check_options: Callable[[BaseModel, RepoType], list[tuple[str, str]]] = _any_options

    def parse_options(self, raw: Mapping[str, Any] | None) -> BaseModel:
        """Validate format options (raises pydantic ``ValidationError``)."""
        return self.options_model.model_validate(dict(raw or {}))


# ---------------------------------------------------------------------------- serving
async def _no_resolve(name: str) -> tuple[RepoConfig, str] | None:
    return None


def _nothing_allowed(action: Action, repo: RepoConfig) -> bool:
    return False


@dataclass
class FormatRequest:
    request: Request
    repo: RepoConfig
    root: Root
    path: str
    principal: Principal
    proxy: ProxyEngine
    hosted: HostedStore  # hosted repositories: receive uploads, serve and delete stored content
    base_url: str
    _require: Callable[[Action], None] = field(repr=False)
    # Another repository's access (cross-repository blob mounts): is ``action`` allowed on it?
    _allows: Callable[[Action, RepoConfig], bool] = field(repr=False, default=_nothing_allowed)
    # Registry names (docker ``/v2/`` routing: prefix, alias, default repository) → (repository, name within it).
    _resolve: Callable[[str], Awaitable[tuple[RepoConfig, str] | None]] = field(repr=False, default=_no_resolve)

    @property
    def method(self) -> str:
        return self.request.method

    @property
    def is_head(self) -> bool:
        return self.request.method == 'HEAD'

    @property
    def client_ip(self) -> str:
        return str(getattr(self.request.state, 'client_ip', '') or '')

    @property
    def repo_url(self) -> str:
        return f'{self.base_url}/repository/{self.repo.name}/'

    def require(self, action: Action) -> None:
        """Authorise an extra action on this repository (raises: 404 when hidden, 401/403 otherwise)."""
        self._require(action)

    def allows(self, action: Action, repo: RepoConfig) -> bool:
        """May this request's principal ``action`` on another repository (decided by ``security.access``)?"""
        return self._allows(action, repo)

    async def resolve_name(self, name: str) -> tuple[RepoConfig, str] | None:
        """Where a registry name (``other-repo/image``, ``docker.io/library/alpine``...) points."""
        return await self._resolve(name)

    def log(self, **fields: Any) -> None:
        """Add fields to this request's access-log line."""
        self.request.state.log.update(fields)


class FormatPlugin(Protocol):
    descriptor: FormatDescriptor

    async def handle(self, req: FormatRequest) -> Response: ...
