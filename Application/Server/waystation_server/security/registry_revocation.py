"""Revoking issued Docker registry tokens before they expire (T-S12; ARCHITECTURE §7).

Registry tokens are stateless JWTs (``security.registry_token``): access tokens live 5 minutes, refresh tokens
(``docker login`` identity tokens) 90 days. A user's tokens are revoked by moving a per-user cut-off forward: every
token issued at or before it (``iat_ms``) is refused.

- MongoDB ``users.tokens_not_before`` is the durable record. ``POST /auth/token`` reads the user fresh when it
  exchanges a refresh token, so a revoked refresh token never yields another access token; ``/v2/`` compares it too
  (through the bearer authenticator's short user cache).
- Valkey ``regtok:nbf:<user id>`` (epoch ms, TTL a little over the access-token lifetime) makes the cut-off reach
  every server process at once: ``/v2/`` reads it on every request that carries a user's registry token (one O(1)
  ``GET``). Valkey down = the MongoDB cut-off only, seen within the user cache lifetime (30 s).

Revoked when: the user is disabled or deleted, their password changes (by themselves or an administrator), or their
sessions are revoked (signing out everywhere else, or an administrator ending them). API tokens need nothing extra:
a registry token issued for an API token carries its id and the API token is re-checked on every use (revoking it
is immediate, ``security.tokens``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from redis.asyncio import Redis

from waystation_common.db.client import Database
from waystation_server.db import users as users_db
from waystation_server.security.registry_token import TOKEN_TTL_S

log = logging.getLogger('waystation.registry_token')

KEY = 'regtok:nbf:'
KEY_TTL_MS = (TOKEN_TTL_S + 60) * 1000


def epoch_ms(at: datetime | None) -> int:
    return int(at.timestamp() * 1000) if at is not None else 0


async def revoke_user(db: Database, valkey: Redis, user_id: str, *, now: datetime | None = None) -> None:
    """Refuse every registry token of ``user_id`` issued until now (access and refresh tokens)."""
    at = now or datetime.now(UTC)
    at = at.replace(microsecond=at.microsecond // 1000 * 1000)  # MongoDB keeps milliseconds
    await users_db.set_tokens_not_before(db, user_id, at)
    try:
        await valkey.set(KEY + user_id, str(epoch_ms(at)), px=KEY_TTL_MS)
    except Exception as exc:  # MongoDB holds it; replicas see it within their user cache lifetime
        log.warning('registry token revocation not announced', extra={'error': type(exc).__name__})


async def not_before_ms(valkey: Redis, subject: str) -> int:
    """The cut-off announced in Valkey for ``subject`` (epoch ms; 0 = none, or Valkey unavailable)."""
    try:
        value = await valkey.get(KEY + subject)
    except Exception:
        return 0
    try:
        return int(value) if value is not None else 0
    except ValueError:
        return 0


def revoked(issued_at_ms: int, *cut_offs: int) -> bool:
    return any(issued_at_ms <= cut for cut in cut_offs if cut)
