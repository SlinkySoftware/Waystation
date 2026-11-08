"""Upstream registry authentication (Docker token authentication spec; FORMATS.md "Docker / OCI" proxy specifics).

``RegistryAuth`` is the proxy engine's ``UpstreamAuthenticator`` for docker repositories:

- The first request of a repository pings the upstream's ``GET /v2/`` (as docker clients do) and remembers its
  challenge, so every upstream request after that carries its token up front; Docker Hub's realm
  (``auth.docker.io``) is known from the start. A ``Bearer realm=...,service=...`` challenge makes it fetch a pull
  token from the realm (anonymous, or with the repository's configured username/password, e.g. Docker Hub or a GHCR
  PAT). A ``401`` on a later request (a realm that differs per image, an expired token) re-learns the challenge
  and retries once.
- Tokens are cached per repository and scope (``repository:<name>:pull``) in process memory until shortly before
  they expire. They are only ever sent to the repository's upstream origin, never to redirect targets (CDNs).
- A ``Basic`` challenge is answered with the configured credentials; a configured bearer secret is sent as is.

Token endpoints are reached through the shared upstream clients (SSRF guard, CA bundle, outbound proxy); token
responses are size-bounded and never logged.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlencode, urlsplit

import httpx

from waystation_common.errors import UpstreamError
from waystation_server.proxy.http import UpstreamClients

log = logging.getLogger('waystation.formats.docker')

DOCKER_HUB_REGISTRY = 'registry-1.docker.io'
DOCKER_HUB_REALM = ('https://auth.docker.io/token', 'registry.docker.io')
TOKEN_MAX_BYTES = 64 * 1024
TOKEN_MAX_CHARS = 16 * 1024
DEFAULT_EXPIRES_S = 60
MAX_TTL_S = 3600
EXPIRY_MARGIN_S = 15
MAX_ENTRIES = 10_000

_PARAM = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))')
_TOKEN_CHARS = re.compile(r'[A-Za-z0-9\-._~+/=]+')

Origin = tuple[str, str, int]


def origin_of(url: str) -> Origin:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or '').lower(), parts.port or (443 if scheme == 'https' else 80)


def parse_challenge(header: str) -> tuple[str, dict[str, str]]:
    """``Bearer realm="https://auth.example.com/token",service="x"`` → ('bearer', {realm, service})."""
    scheme, _, rest = header.strip().partition(' ')
    params = {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3)) for m in _PARAM.finditer(rest)}
    return scheme.lower(), params


@dataclass(frozen=True)
class Realm:
    url: str
    service: str | None


Challenge = Realm | Literal['basic', 'none']  # what the upstream's /v2/ asks for


def challenge_of(header: str) -> Challenge:
    scheme, params = parse_challenge(header)
    if scheme == 'bearer' and params.get('realm'):
        return Realm(params['realm'], params.get('service'))
    return 'basic' if scheme == 'basic' else 'none'


@dataclass
class TokenCache:
    """Process-wide pull tokens and learned challenges, keyed by the repository's identity + config version."""

    clock: Callable[[], float] = time.monotonic
    _tokens: dict[tuple[str, str], tuple[float, str]] = field(default_factory=dict)
    _realms: dict[str, Challenge] = field(default_factory=dict)

    def token(self, repo_key: str, scope: str) -> str | None:
        hit = self._tokens.get((repo_key, scope))
        if hit is None:
            return None
        if hit[0] <= self.clock():
            self._tokens.pop((repo_key, scope), None)
            return None
        return hit[1]

    def put_token(self, repo_key: str, scope: str, token: str, expires_in: int) -> None:
        if len(self._tokens) >= MAX_ENTRIES:
            self._tokens.clear()
        ttl = max(1, min(expires_in, MAX_TTL_S) - EXPIRY_MARGIN_S)
        self._tokens[(repo_key, scope)] = (self.clock() + ttl, token)

    def forget_token(self, repo_key: str, scope: str) -> None:
        self._tokens.pop((repo_key, scope), None)

    def realm(self, repo_key: str) -> Challenge | None:
        return self._realms.get(repo_key)

    def put_realm(self, repo_key: str, realm: Challenge) -> None:
        if len(self._realms) >= MAX_ENTRIES:
            self._realms.clear()
        self._realms[repo_key] = realm


@dataclass
class RegistryAuth:
    cache: TokenCache
    clients: UpstreamClients
    repo_key: str
    upstream_url: str
    scope: str
    allow_private: bool = False
    credentials: Callable[[], tuple[str, str] | None] = lambda: None  # (username, password) for token/basic
    bearer: Callable[[], str | None] = lambda: None  # a static bearer secret (no negotiation)
    _basic_sent: bool = False  # this request already offered the Basic credentials

    @property
    def origin(self) -> Origin:
        return origin_of(self.upstream_url)

    # -------------------------------------------------------------- UpstreamAuthenticator
    async def headers(self, url: str) -> Mapping[str, str]:
        if origin_of(url) != self.origin:
            return {}  # never send upstream credentials to another host (CDN redirects)
        static = self.bearer()
        if static:
            return {'Authorization': f'Bearer {static}'}
        token = self.cache.token(self.repo_key, self.scope)
        if token is not None:
            return {'Authorization': f'Bearer {token}'}
        challenge = self.cache.realm(self.repo_key) or self._known_realm() or await self._ping()
        if challenge == 'basic':
            self._basic_sent = True
            return self._basic_header()
        if isinstance(challenge, Realm):
            token = await self._fetch(challenge, self.scope)
        return {'Authorization': f'Bearer {token}'} if token else {}

    async def challenge(self, response: httpx.Response, url: str) -> bool:
        if origin_of(url) != self.origin or self.bearer():
            return False
        learned = challenge_of(response.headers.get('www-authenticate', ''))
        if learned == 'basic':
            if self.credentials() is None or self._basic_sent:
                return False
            self.cache.put_realm(self.repo_key, 'basic')
            return True
        if not isinstance(learned, Realm):
            return False
        self.cache.put_realm(self.repo_key, learned)
        self.cache.forget_token(self.repo_key, self.scope)
        return await self._fetch(learned, self.scope) is not None

    # -------------------------------------------------------------- internals
    def _known_realm(self) -> Realm | None:
        if self.origin[1] == DOCKER_HUB_REGISTRY:
            return Realm(*DOCKER_HUB_REALM)
        return None

    async def _ping(self) -> Challenge | None:
        """``GET <upstream>/v2/`` without credentials and remember what it asks for. Network failures are not
        remembered (the request then goes out anonymously and a ``401`` still teaches the challenge)."""
        base = self.upstream_url
        url = (base if base.endswith('/') else base + '/') + 'v2/'
        try:
            client = self.clients.client_for(url, allow_private=self.allow_private)
            if self.clients.proxy:
                await self.clients.precheck(url, allow_private=self.allow_private)
            async with client.stream('GET', url, headers={'Accept': 'application/json'}) as response:
                header = response.headers.get('www-authenticate', '') if response.status_code == 401 else ''
        except (httpx.HTTPError, UpstreamError, ValueError) as exc:
            log.warning('upstream registry ping failed', extra={'error': type(exc).__name__})
            return None
        learned = challenge_of(header)
        self.cache.put_realm(self.repo_key, learned)
        return learned

    def _basic_header(self) -> dict[str, str]:
        creds = self.credentials()
        if creds is None:
            return {}
        raw = base64.b64encode(f'{creds[0]}:{creds[1]}'.encode()).decode('ascii')
        return {'Authorization': f'Basic {raw}'}

    async def _fetch(self, realm: Realm, scope: str) -> str | None:
        """Ask the realm for a pull token. Failures return None (the request then goes out without a token and
        the upstream's answer decides)."""
        parts = urlsplit(realm.url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            log.warning('upstream token realm refused', extra={'realm_host': parts.hostname or ''})
            return None
        query = [('scope', scope)]
        if realm.service:
            query.insert(0, ('service', realm.service))
        url = realm.url + ('&' if parts.query else '?') + urlencode(query, safe=':/')
        headers: dict[str, str] = {'Accept': 'application/json'}
        headers.update(self._basic_header())
        try:
            client = self.clients.client_for(url, allow_private=self.allow_private)
            if self.clients.proxy:
                await self.clients.precheck(url, allow_private=self.allow_private)
            async with client.stream('GET', url, headers=headers) as response:
                if response.status_code != 200:
                    log.warning('upstream token request failed', extra={'status': response.status_code})
                    return None
                body = b''
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > TOKEN_MAX_BYTES:
                        log.warning('upstream token response too large')
                        return None
        except (httpx.HTTPError, UpstreamError, ValueError) as exc:
            log.warning('upstream token request failed', extra={'error': type(exc).__name__})
            return None
        return self._store(body, scope)

    def _store(self, body: bytes, scope: str) -> str | None:
        try:
            doc = json.loads(body)
        except ValueError:
            return None
        if not isinstance(doc, dict):
            return None
        token = doc.get('token') or doc.get('access_token')
        if not isinstance(token, str) or not token or len(token) > TOKEN_MAX_CHARS or not _TOKEN_CHARS.fullmatch(token):
            log.warning('upstream token response without a usable token')
            return None
        expires = doc.get('expires_in')
        expires_in = expires if isinstance(expires, int) and not isinstance(expires, bool) and expires > 0 else 0
        self.cache.put_token(self.repo_key, scope, token, expires_in or DEFAULT_EXPIRES_S)
        return token
