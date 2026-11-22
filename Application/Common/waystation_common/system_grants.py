"""Signed system grants: how the worker asks the server to do proxy work for it (adr/0012, T-O12).

The worker never imports the server (ARCHITECTURE §3), but the format protocol logic (resolving a docker tag to its
manifests and layers, an npm packument to its tarball, validation, indexing, coalescing) lives only in the server. So
a prefetch job calls the server's internal prefetch route (``POST /_internal/prefetch/<repo id>``, on
``WAYSTATION_INTERNAL_URL``) and the server does the work exactly as for a client.

The worker proves it is a Waystation process with a **system grant** in the ``X-Ws-System-Grant`` header:
``<expiry>.<mac>``, HMAC-SHA256 under a key derived from each ``WAYSTATION_SECRET_KEYS`` entry (the first signs, all
verify) over a fixed purpose, the repository id and the expiry, valid for ``TTL_S`` seconds. The server
(``waystation_server.security.access.system_grant``) accepts a valid grant only on that route and only as a
``prefetch`` system principal that may read (``GET``) that one proxy repository; writes, other repositories, the REST
API and the format routes never accept it. Without secret keys there are no grants (and no prefetch): the worker and
the server would not share a key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from waystation_common.crypto import parse_key

HEADER = 'X-Ws-System-Grant'
PREFETCH = 'prefetch'
TTL_S = 300
_INFO = b'waystation system grant v1'
_MAX_LEN = 80


class SystemGrants:
    """Sign and verify system grants. ``keys``: the raw ``WAYSTATION_SECRET_KEYS`` (32 bytes each)."""

    def __init__(self, keys: list[bytes]) -> None:
        self._keys = [hmac.new(key, _INFO, hashlib.sha256).digest() for key in keys]

    @classmethod
    def from_secret_keys(cls, values: list[str]) -> SystemGrants:
        return cls([parse_key(value) for value in values])

    @property
    def available(self) -> bool:
        """False without secret keys: nothing can be signed or verified."""
        return bool(self._keys)

    @staticmethod
    def _mac(key: bytes, purpose: str, repo_id: str, expiry: int) -> str:
        message = f'{purpose}\n{repo_id}\n{expiry}'.encode()
        digest = hmac.new(key, message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')

    def sign(self, purpose: str, repo_id: str, *, now: float | None = None) -> str:
        """A grant for ``purpose`` on the repository ``repo_id`` (raises ``RuntimeError`` without keys)."""
        if not self._keys:
            raise RuntimeError('system grants need WAYSTATION_SECRET_KEYS')
        expiry = int(now if now is not None else time.time()) + TTL_S
        return f'{expiry}.{self._mac(self._keys[0], purpose, repo_id, expiry)}'

    def verify(self, grant: str, purpose: str, repo_id: str, *, now: float | None = None) -> bool:
        if not self._keys or len(grant) > _MAX_LEN:
            return False
        expiry_text, sep, mac = grant.partition('.')
        if not sep or not expiry_text.isdigit() or len(expiry_text) > 12:
            return False
        expiry = int(expiry_text)
        current = now if now is not None else time.time()
        if expiry < current or expiry > current + TTL_S + 60:
            return False
        return any(hmac.compare_digest(self._mac(key, purpose, repo_id, expiry), mac) for key in self._keys)
