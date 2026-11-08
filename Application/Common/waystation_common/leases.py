"""Valkey leases for leader election (DATA_MODEL §2 ``lease:<name>``): a key holding the owner's id with a TTL.

``hold_lease`` acquires a free lease or renews one we already hold; it never takes a lease held by someone else.
A crashed holder's lease expires after its TTL. ``release_lease`` deletes it only if we still hold it.
"""

from __future__ import annotations

from redis.asyncio import Redis

# Acquire the lease, or renew it if we already hold it. Returns 1 when held.
_HOLD_LUA = """
local v = redis.call('GET', KEYS[1])
if not v then redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2]) return 1 end
if v == ARGV[1] then redis.call('PEXPIRE', KEYS[1], ARGV[2]) return 1 end
return 0
"""

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


async def hold_lease(redis: Redis, owner: str, *, key: str, ttl_ms: int) -> bool:
    result = await redis.eval(_HOLD_LUA, 1, key, owner, str(ttl_ms))
    return bool(int(result))


async def release_lease(redis: Redis, owner: str, *, key: str) -> bool:
    result = await redis.eval(_RELEASE_LUA, 1, key, owner)
    return bool(int(result))
