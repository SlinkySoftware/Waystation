"""Fixed-window rate limits in Valkey (DATA_MODEL ``rl:<scope>:<id>``)."""

from __future__ import annotations

import hashlib
import logging

from redis.asyncio import Redis

from waystation_common.errors import RateLimited

log = logging.getLogger('waystation.ratelimit')


def key(scope: str, ident: str) -> str:
    # Hash the identifier: usernames and addresses never appear in Valkey key names.
    return f'rl:{scope}:{hashlib.sha256(ident.encode()).hexdigest()[:32]}'


async def check(valkey: Redis, scope: str, ident: str, *, limit: int, window_s: int) -> None:
    """Count one attempt; raise ``RateLimited`` past ``limit`` per ``window_s``. A Valkey outage never blocks."""
    name = key(scope, ident)
    try:
        async with valkey.pipeline(transaction=False) as pipe:
            pipe.incr(name)
            pipe.expire(name, window_s, nx=True)
            pipe.ttl(name)
            count, _, ttl = await pipe.execute()
    except Exception as exc:
        log.warning('rate limit check failed', extra={'error': type(exc).__name__, 'scope': scope})
        return
    if int(count) > limit:
        raise RateLimited('too many attempts; try again later', retry_after=int(ttl) if int(ttl) > 0 else window_s)


async def reset(valkey: Redis, scope: str, ident: str) -> None:
    try:
        await valkey.delete(key(scope, ident))
    except Exception as exc:
        log.warning('rate limit reset failed', extra={'error': type(exc).__name__, 'scope': scope})
