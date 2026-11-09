"""UI sessions (SPECIFICATION §4.9).

- The cookie ``ws_session`` holds a random 256-bit token (HttpOnly, SameSite=Lax, Secure when the base URL is
  https). MongoDB stores only its sha256 (``sessions._id``), so a database dump holds no usable session ids.
- A new session is issued on every sign-in (no fixation: a cookie sent before sign-in is never promoted).
- Absolute lifetime ``WAYSTATION_SESSION_MAX_HOURS`` (``expires_at``, TTL index); idle timeout
  ``WAYSTATION_SESSION_IDLE_MINUTES``. Use is recorded in Valkey (``seen:sessions`` hash, at most once a minute per
  session and process), never written to MongoDB per request (ARCHITECTURE §8); the rollup job flushes it later.
- CSRF: each session has a random token (``csrf``) returned by sign-in and ``/auth/me`` and also set in the readable
  cookie ``ws_csrf``; unsafe methods with a session cookie must echo it in ``X-CSRF-Token`` (double submit).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from starlette.responses import Response

from waystation_common.db.client import Database
from waystation_common.db.seen import SEEN_SESSIONS
from waystation_common.settings import Settings
from waystation_server.db import sessions as sessions_db
from waystation_server.security.identity import SessionRecord

log = logging.getLogger('waystation.sessions')

SESSION_COOKIE = 'ws_session'
CSRF_COOKIE = 'ws_csrf'
CSRF_HEADER = 'x-csrf-token'
SEEN_KEY = SEEN_SESSIONS
TOUCH_EVERY_S = 60.0


def session_id(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def csrf_matches(session: SessionRecord, presented: str | None) -> bool:
    return bool(presented) and hmac.compare_digest(session.csrf.encode(), (presented or '').encode())


class SessionManager:
    def __init__(self, db: Database, valkey: Redis, settings: Settings) -> None:
        self._db = db
        self._valkey = valkey
        self.idle_s = settings.session_idle_minutes * 60
        self.max_s = settings.session_max_hours * 3600
        self.secure = settings.base_url.startswith('https://')
        self._touched: dict[str, float] = {}

    async def create(
        self,
        user_id: str,
        *,
        ip: str | None,
        user_agent: str | None,
        provider_id: str | None = None,
        id_token_enc: str | None = None,
    ) -> tuple[str, SessionRecord]:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        record = SessionRecord(
            id=session_id(token),
            user_id=user_id,
            csrf=secrets.token_urlsafe(24),
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(seconds=self.max_s),
            ip=ip,
            user_agent=(user_agent or '')[:256] or None,
            provider_id=provider_id,
            id_token_enc=id_token_enc,
        )
        await sessions_db.insert(self._db, record)
        self._touched[record.id] = time.time()
        return token, record

    async def _last_seen(self, record: SessionRecord) -> float:
        seen = max(record.last_seen_at.timestamp(), self._touched.get(record.id, 0.0))
        if time.time() - seen <= self.idle_s:
            return seen
        try:
            value = await self._valkey.hget(SEEN_KEY, record.id)
        except Exception as exc:  # Valkey down: fall back to what MongoDB and this process know
            log.warning('session last-seen lookup failed', extra={'error': type(exc).__name__})
            return seen
        return max(seen, float(value)) if value else seen

    async def _touch(self, record: SessionRecord, now: float) -> None:
        if now - self._touched.get(record.id, 0.0) < TOUCH_EVERY_S:
            return
        self._touched[record.id] = now
        if len(self._touched) > 100_000:  # bound the per-process cache
            self._touched.clear()
            self._touched[record.id] = now
        try:
            await self._valkey.hset(SEEN_KEY, record.id, str(int(now)))
        except Exception as exc:
            log.warning('session last-seen update failed', extra={'error': type(exc).__name__})

    async def resolve(self, token: str | None) -> SessionRecord | None:
        """The live session for a cookie value, or None (unknown, expired or idle too long)."""
        if not token or len(token) > 128:
            return None
        record = await sessions_db.get(self._db, session_id(token))
        if record is None:
            return None
        now = time.time()
        if record.expires_at.timestamp() <= now or now - await self._last_seen(record) > self.idle_s:
            await self.destroy(record.id)
            return None
        await self._touch(record, now)
        return record

    async def destroy(self, sid: str) -> None:
        self._touched.pop(sid, None)
        await sessions_db.delete(self._db, sid)
        try:
            await self._valkey.hdel(SEEN_KEY, sid)
        except Exception as exc:
            log.warning('session last-seen cleanup failed', extra={'error': type(exc).__name__})

    def public_id(self, sid: str) -> str:
        """What the sessions API calls a session: derived from (never equal to) its stored id."""
        return hashlib.sha256(f'session:{sid}'.encode()).hexdigest()[:32]

    async def last_seen(self, records: list[SessionRecord]) -> dict[str, datetime]:
        """Each session's last use, including what is still waiting in Valkey for the rollup job."""
        seen = {r.id: max(r.last_seen_at.timestamp(), self._touched.get(r.id, 0.0)) for r in records}
        if records:
            try:
                values = await self._valkey.hmget(SEEN_KEY, [r.id for r in records])
            except Exception as exc:
                log.warning('session last-seen lookup failed', extra={'error': type(exc).__name__})
                values = [None] * len(records)
            for record, raw in zip(records, values, strict=True):
                if raw is not None:
                    try:
                        seen[record.id] = max(seen[record.id], float(raw))
                    except ValueError:
                        continue
        return {sid: datetime.fromtimestamp(ts, UTC) for sid, ts in seen.items()}

    def set_cookies(self, response: Response, token: str, record: SessionRecord) -> None:
        response.set_cookie(
            SESSION_COOKIE, token, max_age=self.max_s, path='/', secure=self.secure, httponly=True, samesite='lax'
        )
        response.set_cookie(
            CSRF_COOKIE, record.csrf, max_age=self.max_s, path='/', secure=self.secure, httponly=False, samesite='lax'
        )

    def clear_cookies(self, response: Response) -> None:
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            response.delete_cookie(name, path='/', secure=self.secure, samesite='lax')
