"""Docker registry tokens (ARCHITECTURE §7, Docker token authentication specification).

``GET /auth/token?service=<host>&scope=repository:<name>:pull,push`` issues a short-lived (5 min) JWT signed with
Ed25519 (``alg: EdDSA``). The caller is anonymous unless it sends HTTP basic credentials (``docker login``: local
username + password; API tokens arrive with T-S04). Each requested scope is granted the subset of its actions the
caller's RBAC grants allow on the repository the name resolves to (SPECIFICATION §4.4 routing); unknown or hidden
names get no actions (the spec's "grant what you can" rule).

On ``/v2/`` the token is the only credential. Its ``sub`` (user id or ``anonymous``) is re-resolved and RBAC is
re-checked on every request: access = token grants ∩ current grants, so revoking a role takes effect immediately.

The signing key lives in ``WAYSTATION_DATA_DIR/keys/registry-token.key``, encrypted with ``WAYSTATION_SECRET_KEYS``
(created by the first server that needs it; every replica uses the same key). Without secret keys the server uses
a per-process key and warns (tokens then only work against the process that issued them).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from waystation_common.crypto import Keyring
from waystation_common.errors import CryptoError

log = logging.getLogger('waystation.registry_token')

TOKEN_TTL_S = 300
KEY_FILE = 'registry-token.key'
KEY_AAD = b'registry-token-signing-key'
ISSUER = 'waystation'
DOCKER_ACTIONS = ('pull', 'push', 'delete')
_SCOPE_ACTIONS = re.compile(r'^[a-z*]+(,[a-z*]+)*$')


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


@dataclass(frozen=True)
class Scope:
    type: str  # 'repository' or 'registry'
    name: str
    actions: tuple[str, ...]

    def claim(self) -> dict[str, Any]:
        return {'type': self.type, 'name': self.name, 'actions': list(self.actions)}


def parse_scopes(values: list[str]) -> list[Scope]:
    """Docker scope grammar: ``type[(class)]:name:actions``; the name may contain ``:`` (registry host:port), the
    actions follow the last ``:``. Several scopes may be space-separated or repeated. Malformed ones are ignored."""
    scopes: list[Scope] = []
    for value in values:
        for item in value.split():
            kind, _, rest = item.partition(':')
            name, _, actions = rest.rpartition(':')
            kind = kind.split('(', 1)[0]
            if kind not in ('repository', 'registry') or not name or not _SCOPE_ACTIONS.fullmatch(actions):
                continue
            if len(name) > 512:
                continue
            scopes.append(Scope(kind, name, tuple(dict.fromkeys(actions.split(',')))))
    return scopes


@dataclass(frozen=True)
class TokenClaims:
    subject: str  # user id, or 'anonymous'
    audience: str
    access: tuple[Scope, ...]
    expires_at: float


class TokenSigner:
    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key
        self.public: Ed25519PublicKey = key.public_key()
        raw = self.public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.kid = hashlib.sha256(raw).hexdigest()[:16]

    def issue(self, *, subject: str, audience: str, access: list[Scope], now: float | None = None) -> tuple[str, int]:
        now = time.time() if now is None else now
        header = {'alg': 'EdDSA', 'typ': 'JWT', 'kid': self.kid}
        payload = {
            'iss': ISSUER,
            'sub': subject,
            'aud': audience,
            'iat': int(now),
            'nbf': int(now) - 5,
            'exp': int(now) + TOKEN_TTL_S,
            'jti': uuid.uuid4().hex,
            'access': [s.claim() for s in access],
        }

        def part(obj: dict[str, Any]) -> str:
            return _b64(json.dumps(obj, separators=(',', ':')).encode())

        signing_input = f'{part(header)}.{part(payload)}'
        signature = self._key.sign(signing_input.encode('ascii'))
        return f'{signing_input}.{_b64(signature)}', TOKEN_TTL_S

    def verify(self, token: str, *, audience: str, now: float | None = None) -> TokenClaims | None:
        """The claims of a valid token for ``audience``; None for anything else (never raises)."""
        parts = token.split('.')
        if len(parts) != 3 or len(token) > 16_384:
            return None
        try:
            header = json.loads(_unb64(parts[0]))
            if header.get('alg') != 'EdDSA' or header.get('kid') != self.kid:
                return None
            self.public.verify(_unb64(parts[2]), f'{parts[0]}.{parts[1]}'.encode('ascii'))
            payload = json.loads(_unb64(parts[1]))
        except ValueError, InvalidSignature, UnicodeError, AttributeError:
            return None
        now = time.time() if now is None else now
        try:
            if payload.get('iss') != ISSUER or payload.get('aud') != audience:
                return None
            if not (int(payload['nbf']) <= now < int(payload['exp'])):
                return None
            access = tuple(
                Scope(str(a['type']), str(a['name']), tuple(str(x) for x in a['actions']))
                for a in payload.get('access', [])
            )
            return TokenClaims(str(payload['sub']), str(payload['aud']), access, float(payload['exp']))
        except KeyError, TypeError, ValueError:
            return None


def _load_or_create(path: Path, keyring: Keyring) -> Ed25519PrivateKey:
    for _attempt in range(50):
        try:
            sealed = path.read_text(encoding='ascii').strip()
        except FileNotFoundError:
            key = Ed25519PrivateKey.generate()
            raw = key.private_bytes(
                serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
            )
            sealed_new = keyring.encrypt(raw, aad=KEY_AAD)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue  # another replica is creating it: read theirs
            with os.fdopen(fd, 'w', encoding='ascii') as handle:
                handle.write(sealed_new + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            log.info('registry token signing key created')
            return key
        try:
            raw = keyring.decrypt(sealed, aad=KEY_AAD)
            return Ed25519PrivateKey.from_private_bytes(raw)
        except CryptoError, ValueError:
            time.sleep(0.1)  # partially written by another replica; retry
    raise CryptoError('cannot read the registry token signing key')


async def load_signer(data_dir: Path, keyring: Keyring | None) -> TokenSigner:
    if keyring is None:
        log.warning('WAYSTATION_SECRET_KEYS is not set: registry tokens use a per-process key')
        return TokenSigner(Ed25519PrivateKey.generate())
    key = await asyncio.to_thread(_load_or_create, data_dir / 'keys' / KEY_FILE, keyring)
    return TokenSigner(key)


class SignerHolder:
    """Loads the signer once, on first use (start-up must not depend on the data dir being writable)."""

    def __init__(self, data_dir: Path, keyring: Keyring | None) -> None:
        self._data_dir = data_dir
        self._keyring = keyring
        self._signer: TokenSigner | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> TokenSigner:
        if self._signer is None:
            async with self._lock:
                if self._signer is None:
                    self._signer = await load_signer(self._data_dir, self._keyring)
        return self._signer
