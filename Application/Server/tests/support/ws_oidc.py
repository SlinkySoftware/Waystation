"""An in-process OpenID provider for OIDC tests: answers discovery, JWKS, token and userinfo requests through the
``security.oidc.Http`` seam and mints ID tokens with real keys (``joserfc``)."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from joserfc import jwt
from joserfc.jwk import ECKey, OKPKey, RSAKey

from waystation_server.security.oidc import HttpResult

ISSUER = 'https://idp.example.com/realms/test'
CLIENT_ID = 'waystation'
CLIENT_SECRET = 'test-client-secret'

Key = RSAKey | ECKey | OKPKey


def rsa_key(kid: str) -> RSAKey:
    return RSAKey.generate_key(2048, parameters={'kid': kid, 'use': 'sig'})


class FakeIdp:
    def __init__(self, issuer: str = ISSUER) -> None:
        self.issuer = issuer
        self.keys: list[Key] = [rsa_key('k1')]
        self.extra_jwks: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str, dict[str, str], dict[str, str]]] = []
        self.codes: dict[str, dict[str, Any]] = {}
        self.discovery_overrides: dict[str, Any] = {}
        self.userinfo_claims: dict[str, Any] = {}
        self.token_status = 200
        self.token_body: dict[str, Any] | None = None
        self.fail: dict[str, int] = {}

    # ---------------------------------------------------------------- documents
    def discovery(self) -> dict[str, Any]:
        base = self.issuer
        doc: dict[str, Any] = {
            'issuer': base,
            'authorization_endpoint': f'{base}/protocol/openid-connect/auth',
            'token_endpoint': f'{base}/protocol/openid-connect/token',
            'jwks_uri': f'{base}/protocol/openid-connect/certs',
            'userinfo_endpoint': f'{base}/protocol/openid-connect/userinfo',
            'end_session_endpoint': f'{base}/protocol/openid-connect/logout',
            'id_token_signing_alg_values_supported': ['RS256', 'ES256', 'EdDSA'],
            'code_challenge_methods_supported': ['plain', 'S256'],
            'scopes_supported': ['openid', 'profile', 'email'],
        }
        doc.update(self.discovery_overrides)
        return doc

    def jwks(self) -> dict[str, Any]:
        return {'keys': [k.as_dict(private=False) for k in self.keys] + self.extra_jwks}

    def claims(self, nonce: str, /, **overrides: Any) -> dict[str, Any]:
        now = int(time.time())
        base: dict[str, Any] = {
            'iss': self.issuer,
            'aud': CLIENT_ID,
            'sub': 'sub-alice',
            'exp': now + 300,
            'iat': now,
            'nonce': nonce,
            'preferred_username': 'alice',
            'name': 'Alice Example',
            'email': 'alice@example.com',
            'groups': ['/developers', 'platform'],
        }
        base.update(overrides)
        return {k: v for k, v in base.items() if v is not None}

    def mint(self, claims: Mapping[str, Any], *, key: Key | None = None, alg: str = 'RS256', **header: Any) -> str:
        key = key or self.keys[0]
        head: dict[str, Any] = {'alg': alg, 'typ': 'JWT'}
        kid = key.kid
        if kid is not None:
            head['kid'] = kid
        head.update(header)
        head = {k: v for k, v in head.items() if v is not None}
        return jwt.encode(head, dict(claims), key, algorithms=[alg])

    def issue_code(self, id_token: str, access_token: str | None = 'at-' + 'x' * 8) -> str:
        code = secrets.token_urlsafe(16)
        self.codes[code] = {'id_token': id_token, 'access_token': access_token, 'token_type': 'Bearer'}
        return code

    # ---------------------------------------------------------------- Http seam
    def count(self, suffix: str) -> int:
        return sum(1 for _, url, _, _ in self.calls if url.endswith(suffix))

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
        self.calls.append((method, url, dict(headers or {}), dict(form or {})))
        path = urlsplit(url).path
        for suffix, status in self.fail.items():
            if path.endswith(suffix):
                return HttpResult(status, 'application/json', b'{}')
        doc = self.discovery()
        if url == self.issuer.rstrip('/') + '/.well-known/openid-configuration':
            return self._json(doc)
        if url == doc['jwks_uri']:
            return self._json(self.jwks())
        if url == doc['token_endpoint'] and method == 'POST':
            return self._token(dict(headers or {}), dict(form or {}))
        if url == doc.get('userinfo_endpoint'):
            return self._json({'sub': 'sub-alice', **self.userinfo_claims})
        return HttpResult(404, 'text/plain', b'not found')

    def _json(self, body: Any, status: int = 200) -> HttpResult:
        return HttpResult(status, 'application/json', json.dumps(body).encode())

    def _token(self, headers: dict[str, str], form: dict[str, str]) -> HttpResult:
        if self.token_body is not None:
            return self._json(self.token_body, self.token_status)
        authorised = headers.get('Authorization', '').startswith('Basic ') or form.get('client_secret')
        if not authorised and form.get('client_id') != CLIENT_ID:
            return self._json({'error': 'invalid_client'}, 401)
        issued = self.codes.pop(form.get('code', ''), None)
        if issued is None or not form.get('code_verifier'):
            return self._json({'error': 'invalid_grant'}, 400)
        return self._json(issued)


def query_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


class Browser:
    """An already used httpx client as an async context manager (closes it on exit)."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def __aenter__(self) -> httpx.AsyncClient:
        return self.client

    async def __aexit__(self, *exc: object) -> None:
        await self.client.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()

    def __getattr__(self, name: str) -> Any:  # get, post, cookies... of the client
        return getattr(self.client, name)
