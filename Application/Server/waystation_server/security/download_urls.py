"""Signed download URLs (adr/0009, proposed; T-P5-TFM).

Terraform and OpenTofu send their registry credentials (the ``credentials`` block: a bearer token) only to the
registry API. The files a registry answer points at (a module archive in ``X-Terraform-Get``, a provider's zip,
``SHA256SUMS`` and signature in a download document) are fetched without them, as private registries hand out
pre-signed URLs. So when the principal asking for such an answer is signed in, the Terraform format appends a
**download grant** to each URL it gives out: ``?ws_grant=<expiry>.<mac>``, an HMAC-SHA256 (under a key derived from
the first ``WAYSTATION_SECRET_KEYS`` entry; every entry is tried when verifying) over the repository id, the exact
normalised path and the expiry, valid for ``TTL_S`` seconds.

A request carrying a grant that verifies for its repository and path is a ``GET``/``HEAD`` of that one file with no
other rights (``security.access.download_grant``); anything else (another path, expired, forged) is ignored and the
request is authenticated as usual. The grant holds no identity and no secret beyond itself; the server's access log
records paths without query strings. Without secret keys a random per-process key is used (grants then work only on
the replica that issued them).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from waystation_common.crypto import parse_key
from waystation_common.settings import Settings

PARAM = 'ws_grant'
TTL_S = 300
_INFO = b'waystation download url v1'
_MAX_LEN = 64


class DownloadUrls:
    def __init__(self, keys: list[bytes]) -> None:
        material = keys or [secrets.token_bytes(32)]
        self._keys = [hmac.new(key, _INFO, hashlib.sha256).digest() for key in material]

    @classmethod
    def from_settings(cls, settings: Settings) -> DownloadUrls:
        return cls([parse_key(value.get_secret_value()) for value in settings.secret_keys])

    @staticmethod
    def _mac(key: bytes, repo_id: str, path: str, expiry: int) -> str:
        message = f'{repo_id}\n{path}\n{expiry}'.encode()
        digest = hmac.new(key, message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')

    def sign(self, repo_id: str, path: str, *, now: float | None = None) -> str:
        """A grant for ``GET``/``HEAD`` of ``path`` (normalised, no leading slash) in the repository ``repo_id``."""
        expiry = int(now if now is not None else time.time()) + TTL_S
        return f'{expiry}.{self._mac(self._keys[0], repo_id, path, expiry)}'

    def verify(self, grant: str, repo_id: str, path: str, *, now: float | None = None) -> bool:
        if len(grant) > _MAX_LEN:
            return False
        expiry_text, sep, mac = grant.partition('.')
        if not sep or not expiry_text.isdigit() or len(expiry_text) > 12:
            return False
        expiry = int(expiry_text)
        current = now if now is not None else time.time()
        if expiry < current or expiry > current + TTL_S + 60:
            return False
        return any(hmac.compare_digest(self._mac(key, repo_id, path, expiry), mac) for key in self._keys)
