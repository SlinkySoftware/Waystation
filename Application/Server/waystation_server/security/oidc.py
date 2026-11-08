"""OpenID Connect relying party (SPECIFICATION §4.9, T-A02): discovery, authorization code + PKCE, ID token checks.

The browser flow itself (state, cookies, sessions) lives in ``waystation_server.oidc_auth``; this module is the
protocol side and never touches MongoDB, Valkey or a request.

- **HTTP**: every call to the identity provider goes through the server's pooled upstream clients and the SSRF guard
  (``allow_private`` per provider, like ``allow_private_upstream``), with the provider's timeout, no redirects and a
  size bound on every body. Nothing blocks the event loop (signature checks on a few-KiB token are microseconds).
- **Discovery** (``<issuer>/.well-known/openid-configuration``) is cached per issuer for an hour. The document must
  name exactly the configured issuer; the authorization, token and JWKS endpoints are required; endpoints must be
  https unless the issuer itself is http (a private test provider).
- **JWKS** is cached per URI for an hour and fetched again when a token names a key id it does not hold (key
  rotation), at most once every ``JWKS_MIN_REFRESH_S``. Encryption keys (``use: enc``) are never used.
- **ID token**: compact JWS only; ``alg`` must be in the provider's allowlist, which can only contain asymmetric
  algorithms (``none`` and HMAC are refused, so a public key can never be used as an HMAC secret); the key must
  match the algorithm's key type (and its own ``alg`` when it states one); ``crit`` headers are refused. Claims:
  ``iss`` = the issuer, ``aud`` contains the client id (several audiences need ``azp`` = the client id; ``azp`` when
  present must be the client id), ``exp`` in the future, ``iat`` not in the future and not older than a day,
  ``nbf`` passed, ``nonce`` = the one sent, ``sub`` present. Clock leeway ``LEEWAY_S``.
- **Claims → identity**: the username, display name and email claims (dotted paths allowed, e.g.
  ``realm_access.roles``) and the groups claim (a list, or one string). Usernames are lower-cased. When the username
  claim is ``email`` the provider must not say ``email_verified: false``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urlsplit

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey, JWKRegistry, OKPKey, RSAKey

from waystation_common.errors import UpstreamError
from waystation_server.proxy.http import UpstreamClients
from waystation_server.proxy.ssrf import UpstreamBlocked
from waystation_server.security.external import ExternalGroup, ExternalIdentity
from waystation_server.security.providers import (
    OIDC_SIGNING_ALGS,
    OidcClientAuth,
    OidcConfigBase,
    group_key,
)

log = logging.getLogger('waystation.auth.oidc')

DISCOVERY_PATH = '/.well-known/openid-configuration'
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_ID_TOKEN_BYTES = 16 * 1024
MAX_KEYS = 100
MAX_GROUPS = 500
MAX_GROUP_LENGTH = 1024
MAX_USERNAME = 256
DISCOVERY_TTL_S = 3600.0
JWKS_TTL_S = 3600.0
JWKS_MIN_REFRESH_S = 10.0
LEEWAY_S = 60
MAX_TOKEN_AGE_S = 86400
_PROTECTED_CLAIMS = frozenset({'iss', 'sub', 'aud', 'exp', 'iat', 'nbf', 'nonce', 'azp', 'auth_time', 'at_hash'})

Key = RSAKey | ECKey | OKPKey


class OidcError(Exception):
    """A provider or token problem. ``reason`` is a short code for logs and tests; ``message`` is for admins."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f'{reason}: {message}')
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------- HTTP
@dataclass(frozen=True)
class HttpResult:
    status: int
    content_type: str
    body: bytes
    latency_ms: float = 0.0

    def json(self) -> dict[str, Any]:
        try:
            data = json.loads(self.body)
        except ValueError:
            raise OidcError('bad_response', 'the provider did not answer with JSON') from None
        if not isinstance(data, dict):
            raise OidcError('bad_response', 'the provider did not answer with a JSON object')
        return data


class Http(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        allow_private: bool,
        timeout_s: float,
        headers: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None,
    ) -> HttpResult: ...


class UpstreamHttp:
    """``Http`` on the server's pooled upstream clients (SSRF guard at connect time, no redirects)."""

    def __init__(self, clients: UpstreamClients) -> None:
        self._clients = clients

    async def request(
        self,
        method: str,
        url: str,
        *,
        allow_private: bool,
        timeout_s: float,
        headers: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None,
    ) -> HttpResult:
        host = urlsplit(url).hostname or url
        try:
            await self._clients.precheck(url, allow_private=allow_private)
            client = self._clients.client_for(url, allow_private=allow_private)
            started = time.perf_counter()
            async with client.stream(
                method,
                url,
                headers={'Accept': 'application/json', **(headers or {})},
                data=dict(form) if form is not None else None,
                timeout=httpx.Timeout(timeout_s),
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_DOCUMENT_BYTES:
                        raise OidcError('too_large', f'{host} sent more than {MAX_DOCUMENT_BYTES} bytes')
                latency = round((time.perf_counter() - started) * 1000, 1)
                content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
                return HttpResult(response.status_code, content_type, bytes(body), latency)
        except UpstreamBlocked:
            raise OidcError('blocked', f'{host} resolves to a private or reserved address (allow private)') from None
        except (UpstreamError, ValueError) as exc:
            raise OidcError('unreachable', str(exc) or f'cannot reach {host}') from None
        except httpx.TimeoutException:
            raise OidcError('timeout', f'{host} did not answer in {timeout_s:g} s') from None
        except httpx.HTTPError as exc:
            tls = 'SSL' in repr(exc) or 'certificate' in str(exc).lower()
            reason = 'tls' if tls else 'unreachable'
            raise OidcError(reason, f'{host}: {"TLS verification failed" if tls else type(exc).__name__}') from None


# ---------------------------------------------------------------------------- discovery
@dataclass(frozen=True)
class Metadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None = None
    end_session_endpoint: str | None = None
    signing_algs: tuple[str, ...] | None = None  # ``id_token_signing_alg_values_supported``
    code_challenge_methods: tuple[str, ...] | None = None
    scopes_supported: tuple[str, ...] | None = None
    latency_ms: float = 0.0


def discovery_url(issuer: str) -> str:
    return issuer.rstrip('/') + DISCOVERY_PATH


def check_issuer(issuer: str, *, allow_private: bool) -> str | None:
    """Why the issuer URL is unusable, or None."""
    parts = urlsplit(issuer)
    if parts.scheme not in ('https', 'http') or not parts.hostname:
        return 'an https URL with a host'
    if parts.scheme == 'http' and not allow_private:
        return 'http is only allowed for private providers (allow private addresses)'
    if parts.username or parts.password or parts.query or parts.fragment:
        return 'no credentials, query or fragment'
    return None


def _endpoint(doc: dict[str, Any], name: str, *, secure: bool, required: bool) -> str | None:
    value = doc.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise OidcError('discovery', f'the discovery document has no usable {name}')
    parts = urlsplit(value)
    if parts.scheme not in ('https', 'http') or not parts.hostname or parts.fragment or parts.username:
        raise OidcError('discovery', f'{name} is not an http(s) URL')
    if secure and parts.scheme != 'https':
        raise OidcError('discovery', f'{name} must be https')
    return value


def _strings(doc: dict[str, Any], name: str) -> tuple[str, ...] | None:
    value = doc.get(name)
    if not isinstance(value, list):
        return None
    return tuple(str(v) for v in value[:100] if isinstance(v, str))


def parse_metadata(doc: dict[str, Any], issuer: str, latency_ms: float = 0.0) -> Metadata:
    stated = doc.get('issuer')
    if stated != issuer:
        raise OidcError('issuer_mismatch', f'the provider states its issuer as {str(stated)[:200]!r}, not {issuer!r}')
    secure = urlsplit(issuer).scheme == 'https'
    authorization = _endpoint(doc, 'authorization_endpoint', secure=secure, required=True)
    token = _endpoint(doc, 'token_endpoint', secure=secure, required=True)
    jwks = _endpoint(doc, 'jwks_uri', secure=secure, required=True)
    assert authorization is not None
    assert token is not None
    assert jwks is not None
    return Metadata(
        issuer=issuer,
        authorization_endpoint=authorization,
        token_endpoint=token,
        jwks_uri=jwks,
        userinfo_endpoint=_endpoint(doc, 'userinfo_endpoint', secure=secure, required=False),
        end_session_endpoint=_endpoint(doc, 'end_session_endpoint', secure=secure, required=False),
        signing_algs=_strings(doc, 'id_token_signing_alg_values_supported'),
        code_challenge_methods=_strings(doc, 'code_challenge_methods_supported'),
        scopes_supported=_strings(doc, 'scopes_supported'),
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------- keys
@dataclass(frozen=True)
class SigningKey:
    kid: str | None
    alg: str | None
    key: Key


@dataclass
class KeyCache:
    keys: tuple[SigningKey, ...]
    fetched_at: float


def parse_jwks(doc: dict[str, Any]) -> tuple[SigningKey, ...]:
    raw = doc.get('keys')
    if not isinstance(raw, list):
        raise OidcError('jwks', 'the JWKS document has no keys')
    keys: list[SigningKey] = []
    for item in raw[:MAX_KEYS]:
        if not isinstance(item, dict) or item.get('use', 'sig') != 'sig' or item.get('kty') not in ('RSA', 'EC', 'OKP'):
            continue
        public = {k: v for k, v in item.items() if k not in ('d', 'p', 'q', 'dp', 'dq', 'qi', 'oth', 'key_ops')}
        try:
            key = JWKRegistry.import_key(public)
        except (JoseError, ValueError, TypeError) as exc:
            log.info('OIDC JWKS key skipped', extra={'kid': str(item.get('kid'))[:64], 'error': type(exc).__name__})
            continue
        if not isinstance(key, RSAKey | ECKey | OKPKey):  # pragma: no cover - kty filtered above
            continue
        kid = item.get('kid')
        alg = item.get('alg')
        keys.append(SigningKey(str(kid) if kid is not None else None, str(alg) if alg is not None else None, key))
    return tuple(keys)


_EC_CURVES = {'ES256': 'P-256', 'ES384': 'P-384', 'ES512': 'P-521'}


def key_fits(key: SigningKey, alg: str) -> bool:
    """The key's type (and curve, and its own ``alg`` when stated) suits ``alg``: no algorithm confusion."""
    if key.alg is not None and key.alg != alg:
        return False
    if alg[:2] in ('RS', 'PS'):
        return isinstance(key.key, RSAKey)
    if alg in _EC_CURVES:
        return isinstance(key.key, ECKey) and key.key.curve_name == _EC_CURVES[alg]
    if alg == 'Ed25519':  # RFC 9864 fully specified
        return isinstance(key.key, OKPKey) and key.key.curve_name == 'Ed25519'
    if alg == 'EdDSA':
        return isinstance(key.key, OKPKey) and key.key.curve_name in ('Ed25519', 'Ed448')
    return False


def select_key(keys: tuple[SigningKey, ...], alg: str, kid: str | None) -> SigningKey | None:
    fitting = [k for k in keys if key_fits(k, alg)]
    if kid is not None:
        return next((k for k in fitting if k.kid == kid), None)
    return fitting[0] if len(fitting) == 1 else None


# ---------------------------------------------------------------------------- tokens
def _b64json(segment: str) -> dict[str, Any]:
    try:
        data = json.loads(base64.urlsafe_b64decode(segment + '=' * (-len(segment) % 4)))
    except ValueError:
        raise OidcError('id_token', 'malformed token') from None
    if not isinstance(data, dict):
        raise OidcError('id_token', 'malformed token')
    return data


def token_header(token: str) -> dict[str, Any]:
    if len(token) > MAX_ID_TOKEN_BYTES:
        raise OidcError('id_token', 'token too large')
    parts = token.split('.')
    if len(parts) != 3 or not all(parts):  # compact JWS only (no JWE, no unsigned token)
        raise OidcError('id_token', 'not a signed JWT')
    return _b64json(parts[0])


def check_claims(claims: dict[str, Any], cfg: OidcConfigBase, *, issuer: str, nonce: str, now: float) -> None:
    if claims.get('iss') != issuer:
        raise OidcError('iss', 'issuer does not match')
    aud = claims.get('aud')
    audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    if cfg.client_id not in audiences:
        raise OidcError('aud', 'token not issued for this client')
    azp = claims.get('azp')
    if (len(audiences) > 1 and azp is None) or (azp is not None and azp != cfg.client_id):
        raise OidcError('azp', 'token authorised for another party')
    exp, iat, nbf = claims.get('exp'), claims.get('iat'), claims.get('nbf')
    if not isinstance(exp, int | float) or isinstance(exp, bool) or exp + LEEWAY_S <= now:
        raise OidcError('exp', 'token expired')
    if not isinstance(iat, int | float) or isinstance(iat, bool) or iat - LEEWAY_S > now or iat < now - MAX_TOKEN_AGE_S:
        raise OidcError('iat', 'token issued at an unusable time')
    if nbf is not None and (not isinstance(nbf, int | float) or nbf - LEEWAY_S > now):
        raise OidcError('nbf', 'token not valid yet')
    presented = claims.get('nonce')
    if not isinstance(presented, str) or not hmac.compare_digest(presented.encode(), nonce.encode()):
        raise OidcError('nonce', 'nonce does not match')
    sub = claims.get('sub')
    if not isinstance(sub, str) or not sub or len(sub) > 255:
        raise OidcError('sub', 'no subject')


# ---------------------------------------------------------------------------- claims → identity
def claim(claims: Mapping[str, Any], name: str) -> Any:
    """A claim by name, or by dotted path (``realm_access.roles``) when no claim has the literal name."""
    if name in claims:
        return claims[name]
    value: Any = claims
    for part in name.split('.'):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _text(value: Any, limit: int) -> str | None:
    if isinstance(value, str) and value.strip() and len(value) <= limit:
        return value.strip()
    return None


def groups_from(value: Any) -> tuple[ExternalGroup, ...]:
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    groups: dict[str, ExternalGroup] = {}
    for item in values:
        text = _text(item, MAX_GROUP_LENGTH)
        if text is None:
            continue
        # Keycloak's full group paths (``/platform/ops``) also match their last segment (``ops``).
        name = text.strip('/').rsplit('/', 1)[-1] if text.startswith('/') and text.strip('/') else text
        groups.setdefault(group_key(text), ExternalGroup(id=text, name=name))
        if len(groups) >= MAX_GROUPS:
            break
    return tuple(groups.values())


def identity_from(source: str, cfg: OidcConfigBase, claims: Mapping[str, Any]) -> ExternalIdentity:
    username = _text(claim(claims, cfg.username_claim), MAX_USERNAME)
    if username is None or not username.isprintable():
        raise OidcError('username', f'no usable {cfg.username_claim!r} claim')
    if cfg.username_claim == 'email' and claims.get('email_verified') is False:
        raise OidcError('email_unverified', 'the email address used as username is not verified')
    groups = groups_from(claim(claims, cfg.groups_claim)) if cfg.groups_claim else ()
    if cfg.require_group:
        wanted = group_key(cfg.require_group)
        if not any(wanted in (group_key(g.id), group_key(g.name)) for g in groups):
            raise OidcError('require_group', 'not a member of the required group')
    return ExternalIdentity(
        provider=source,
        subject=str(claims['sub']),
        username=username.lower(),
        display_name=_text(claim(claims, cfg.display_name_claim), 256) or username,
        email=_text(claim(claims, cfg.email_claim), 320),
        groups=groups,
    )


# ---------------------------------------------------------------------------- flow helpers
@dataclass(frozen=True)
class FlowSecrets:
    state: str
    nonce: str
    verifier: str

    @classmethod
    def new(cls) -> FlowSecrets:
        return cls(secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48))

    @property
    def challenge(self) -> str:
        digest = hashlib.sha256(self.verifier.encode('ascii')).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def _with_query(url: str, params: dict[str, str]) -> str:
    return url + ('&' if urlsplit(url).query else '?') + urlencode(params)


def scopes_of(cfg: OidcConfigBase) -> str:
    return ' '.join(dict.fromkeys(['openid', *cfg.scopes]))


def authorization_url(meta: Metadata, cfg: OidcConfigBase, flow: FlowSecrets, *, redirect_uri: str) -> str:
    return _with_query(
        meta.authorization_endpoint,
        {
            'response_type': 'code',
            'client_id': cfg.client_id,
            'redirect_uri': redirect_uri,
            'scope': scopes_of(cfg),
            'state': flow.state,
            'nonce': flow.nonce,
            'code_challenge': flow.challenge,
            'code_challenge_method': 'S256',
        },
    )


def end_session_url(
    meta: Metadata, cfg: OidcConfigBase, *, id_token_hint: str | None, post_logout_redirect_uri: str
) -> str | None:
    if not cfg.rp_logout or not meta.end_session_endpoint:
        return None
    params = {'client_id': cfg.client_id, 'post_logout_redirect_uri': post_logout_redirect_uri}
    if id_token_hint:
        params['id_token_hint'] = id_token_hint
    return _with_query(meta.end_session_endpoint, params)


@dataclass(frozen=True)
class TokenResponse:
    id_token: str
    access_token: str | None


@dataclass(frozen=True)
class SignIn:
    identity: ExternalIdentity
    id_token: str
    claims: dict[str, Any] = field(default_factory=dict)


class OidcClient:
    """Discovery and JWKS caches plus the protocol steps. One per server process (``Services.oidc``)."""

    def __init__(self, http: Http, *, clock: Callable[[], float] = time.time) -> None:
        self._http = http
        self._clock = clock
        self._discovery: dict[tuple[str, bool], tuple[float, Metadata]] = {}
        self._jwks: dict[tuple[str, bool], KeyCache] = {}

    def forget(self) -> None:
        self._discovery.clear()
        self._jwks.clear()

    async def _get_json(self, url: str, cfg: OidcConfigBase, what: str) -> tuple[dict[str, Any], float]:
        result = await self._http.request('GET', url, allow_private=cfg.allow_private, timeout_s=cfg.timeout_s)
        if result.status != 200:
            raise OidcError(what, f'{what} answered HTTP {result.status}')
        return result.json(), result.latency_ms

    async def metadata(self, cfg: OidcConfigBase, *, fresh: bool = False) -> Metadata:
        key = (cfg.issuer, cfg.allow_private)
        cached = self._discovery.get(key)
        now = time.monotonic()
        if cached and not fresh and now - cached[0] < DISCOVERY_TTL_S:
            return cached[1]
        problem = check_issuer(cfg.issuer, allow_private=cfg.allow_private)
        if problem:
            raise OidcError('issuer', f'issuer must be {problem}')
        doc, latency = await self._get_json(discovery_url(cfg.issuer), cfg, 'discovery')
        meta = parse_metadata(doc, cfg.issuer, latency)
        self._discovery[key] = (now, meta)
        return meta

    async def keys(self, meta: Metadata, cfg: OidcConfigBase, *, refresh: bool = False) -> tuple[SigningKey, ...]:
        key = (meta.jwks_uri, cfg.allow_private)
        cached = self._jwks.get(key)
        now = time.monotonic()
        # A refresh (unknown key id) refetches unless the keys were fetched moments ago.
        if cached is not None and now - cached.fetched_at < (JWKS_MIN_REFRESH_S if refresh else JWKS_TTL_S):
            return cached.keys
        doc, _ = await self._get_json(meta.jwks_uri, cfg, 'jwks')
        keys = parse_jwks(doc)
        self._jwks[key] = KeyCache(keys, now)
        log.info('OIDC keys fetched', extra={'jwks_uri': meta.jwks_uri, 'keys': len(keys)})
        return keys

    async def exchange(
        self, meta: Metadata, cfg: OidcConfigBase, secret: str | None, *, code: str, redirect_uri: str, verifier: str
    ) -> TokenResponse:
        form = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri}
        if verifier:
            form['code_verifier'] = verifier
        headers = client_auth(cfg, secret, form)
        result = await self._http.request(
            'POST', meta.token_endpoint, allow_private=cfg.allow_private, timeout_s=cfg.timeout_s, headers=headers,
            form=form,
        )  # fmt: skip
        body = result.json() if result.content_type.endswith('json') or result.body[:1] == b'{' else {}
        if result.status != 200:
            error = str(body.get('error', ''))[:64] or f'HTTP {result.status}'
            raise OidcError('token', f'the token endpoint refused the code ({error})')
        id_token = body.get('id_token')
        if not isinstance(id_token, str) or not id_token:
            raise OidcError('token', 'the token response has no ID token (is the openid scope allowed?)')
        access = body.get('access_token')
        return TokenResponse(id_token=id_token, access_token=access if isinstance(access, str) and access else None)

    async def verify_id_token(self, token: str, meta: Metadata, cfg: OidcConfigBase, *, nonce: str) -> dict[str, Any]:
        header = token_header(token)
        alg, kid = header.get('alg'), header.get('kid')
        if not isinstance(alg, str) or alg not in OIDC_SIGNING_ALGS or alg not in cfg.signing_algs:
            raise OidcError('alg', f'signature algorithm {str(alg)[:16]!r} is not accepted')
        if 'crit' in header:
            raise OidcError('id_token', 'critical header extensions are not supported')
        if kid is not None and not isinstance(kid, str):
            raise OidcError('id_token', 'malformed key id')
        signing = select_key(await self.keys(meta, cfg), alg, kid)
        if signing is None:  # unknown key id: the provider may have rotated its keys
            signing = select_key(await self.keys(meta, cfg, refresh=True), alg, kid)
        if signing is None:
            raise OidcError('kid', 'no matching signing key')
        try:
            decoded = jwt.decode(token, signing.key, algorithms=[alg])
        except JoseError:
            raise OidcError('signature', 'bad signature') from None
        claims = dict(decoded.claims)
        check_claims(claims, cfg, issuer=meta.issuer, nonce=nonce, now=self._clock())
        return claims

    async def userinfo(self, meta: Metadata, cfg: OidcConfigBase, access_token: str, sub: str) -> dict[str, Any]:
        if not meta.userinfo_endpoint:
            raise OidcError('userinfo', 'the provider has no userinfo endpoint')
        result = await self._http.request(
            'GET',
            meta.userinfo_endpoint,
            allow_private=cfg.allow_private,
            timeout_s=cfg.timeout_s,
            headers={'Authorization': f'Bearer {access_token}'},
        )
        if result.status != 200:
            raise OidcError('userinfo', f'userinfo answered HTTP {result.status}')
        info = result.json()
        if info.get('sub') != sub:
            raise OidcError('userinfo', 'userinfo is about another subject')
        return info

    async def sign_in(
        self,
        source: str,
        cfg: OidcConfigBase,
        secret: str | None,
        *,
        code: str,
        redirect_uri: str,
        flow_nonce: str,
        verifier: str,
    ) -> SignIn:
        meta = await self.metadata(cfg)
        tokens = await self.exchange(meta, cfg, secret, code=code, redirect_uri=redirect_uri, verifier=verifier)
        claims = await self.verify_id_token(tokens.id_token, meta, cfg, nonce=flow_nonce)
        if cfg.use_userinfo and tokens.access_token:
            info = await self.userinfo(meta, cfg, tokens.access_token, claims['sub'])
            for name, value in info.items():
                if name not in _PROTECTED_CLAIMS:
                    claims.setdefault(name, value)
        return SignIn(identity=identity_from(source, cfg, claims), id_token=tokens.id_token, claims=claims)

    async def diagnose(self, cfg: OidcConfigBase, secret: str | None, *, redirect_uri: str) -> Diagnosis:
        """ "Test discovery": fetch and check discovery and keys, and the client credentials (a token request with a
        code that cannot exist: ``invalid_grant`` means the provider accepted the client)."""
        result = Diagnosis()
        try:
            meta = await self.metadata(cfg, fresh=True)
        except OidcError as exc:
            result.checks.append(Check('discovery', False, exc.message))
            return result
        result.metadata = meta
        result.latency_ms = meta.latency_ms
        result.checks.append(Check('discovery', True, f'issuer {meta.issuer}'))
        try:
            keys = await self.keys(meta, cfg, refresh=True)
            usable = [k for k in keys if any(key_fits(k, alg) for alg in cfg.signing_algs)]
            result.keys = len(usable)
            message = f'{len(usable)} usable signing key(s)' if usable else 'no key fits the accepted algorithms'
            result.checks.append(Check('jwks', bool(usable), message))
        except OidcError as exc:
            result.checks.append(Check('jwks', False, exc.message))
        if meta.signing_algs is not None:
            common = [a for a in cfg.signing_algs if a in meta.signing_algs]
            offered = ', '.join(meta.signing_algs[:10]) or 'none'
            result.checks.append(
                Check('algorithms', bool(common), ', '.join(common) if common else f'the provider signs with {offered}')
            )
        methods = meta.code_challenge_methods
        if methods is None or 'S256' in methods:
            result.checks.append(Check('pkce', True, 'S256' if methods else 'not advertised; S256 is sent anyway'))
        else:
            result.checks.append(Check('pkce', False, 'the provider does not offer S256'))
        if cfg.use_userinfo:
            ok = meta.userinfo_endpoint is not None
            result.checks.append(Check('userinfo', ok, 'available' if ok else 'no userinfo endpoint'))
        if cfg.rp_logout:
            ok = meta.end_session_endpoint is not None
            result.checks.append(Check('logout', True, 'supported' if ok else 'not offered: sign-out stays local'))
        result.checks.append(await self._check_client(meta, cfg, secret, redirect_uri=redirect_uri))
        return result

    async def _check_client(
        self, meta: Metadata, cfg: OidcConfigBase, secret: str | None, *, redirect_uri: str
    ) -> Check:
        form = {
            'grant_type': 'authorization_code',
            'code': 'waystation-connection-test-' + secrets.token_urlsafe(16),
            'redirect_uri': redirect_uri,
            'code_verifier': secrets.token_urlsafe(48),
        }
        headers = client_auth(cfg, secret, form)
        try:
            result = await self._http.request(
                'POST', meta.token_endpoint, allow_private=cfg.allow_private, timeout_s=cfg.timeout_s,
                headers=headers, form=form,
            )  # fmt: skip
            error = str(result.json().get('error', '')) if result.body[:1] == b'{' else ''
        except OidcError as exc:
            return Check('client', False, exc.message)
        if error == 'invalid_grant':
            return Check('client', True, 'client credentials accepted')
        if error in ('invalid_client', 'unauthorized_client') or result.status == 401:
            return Check('client', False, f'the provider refused the client ({error or "HTTP 401"})')
        return Check('client', True, f'could not confirm the client credentials ({error or result.status})')


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    message: str


@dataclass
class Diagnosis:
    checks: list[Check] = field(default_factory=list)
    metadata: Metadata | None = None
    keys: int = 0
    latency_ms: float | None = None

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def client_auth(cfg: OidcConfigBase, secret: str | None, form: dict[str, str]) -> dict[str, str]:
    """Token endpoint client authentication (RFC 6749 §2.3.1); adds to ``form`` for ``client_secret_post``/public."""
    if cfg.client_auth is OidcClientAuth.CLIENT_SECRET_BASIC and secret:
        raw = f'{quote(cfg.client_id, safe="")}:{quote(secret, safe="")}'.encode()
        return {'Authorization': 'Basic ' + base64.b64encode(raw).decode('ascii')}
    form['client_id'] = cfg.client_id
    if cfg.client_auth is OidcClientAuth.CLIENT_SECRET_POST and secret:
        form['client_secret'] = secret
    return {}
