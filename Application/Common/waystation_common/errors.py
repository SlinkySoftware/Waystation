"""Error types shared by server and worker.

Every domain error derives from ``WaystationError`` and carries a stable machine ``code`` and the HTTP ``status`` it
maps to. The server turns them into the format's native error shape or RFC 9457 problem+json (ARCHITECTURE §12);
``detail`` must be safe to show to a client (never a secret, never an internal path).
"""

from __future__ import annotations

from typing import ClassVar


class WaystationError(Exception):
    code: ClassVar[str] = 'internal'
    status: ClassVar[int] = 500

    def __init__(self, detail: str = '') -> None:
        super().__init__(detail or self.code)
        self.detail = detail or self.code


class ConfigError(WaystationError):
    """Invalid process configuration (raised at start-up)."""

    code = 'config'


class ValidationFailed(WaystationError):
    code = 'invalid'
    status = 400


class PathRejected(ValidationFailed):
    """A path failed normalisation (traversal, absolute, NUL, bad encoding...)."""

    code = 'invalid_path'


class Unauthorized(WaystationError):
    code = 'unauthorized'
    status = 401


class Forbidden(WaystationError):
    code = 'forbidden'
    status = 403


class NotFound(WaystationError):
    code = 'not_found'
    status = 404


class Conflict(WaystationError):
    """``field``: the request field that conflicts (REST answers then carry it in ``errors[]``)."""

    code = 'conflict'
    status = 409

    def __init__(self, detail: str = '', *, field: str | None = None) -> None:
        super().__init__(detail)
        self.field = field


class RateLimited(WaystationError):
    """Too many attempts; ``retry_after`` seconds until the next one is allowed (sent as ``Retry-After``)."""

    code = 'rate_limited'
    status = 429

    def __init__(self, detail: str = '', *, retry_after: int = 60) -> None:
        super().__init__(detail)
        self.retry_after = max(1, retry_after)
        self.headers = {'Retry-After': str(self.retry_after)}


class PayloadTooLarge(WaystationError):
    code = 'too_large'
    status = 413


class DigestMismatch(WaystationError):
    """Content did not match the digest/size the protocol promised (ARCHITECTURE §6.4)."""

    code = 'digest_mismatch'
    status = 502


class UpstreamError(WaystationError):
    code = 'upstream'
    status = 502


class InsufficientStorage(WaystationError):
    """Free-space guard tripped (SPECIFICATION §4.8)."""

    code = 'insufficient_storage'
    status = 507


class CryptoError(WaystationError):
    """Encryption/decryption failure (bad key, tampered or foreign ciphertext)."""

    code = 'crypto'
