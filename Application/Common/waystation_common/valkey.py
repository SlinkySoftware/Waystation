"""Valkey client factory (redis-py asyncio client; ARCHITECTURE §2). Creating a client does not connect."""

from __future__ import annotations

from redis.asyncio import Redis

from waystation_common.settings import Settings


def create_valkey(settings: Settings, *, decode_responses: bool = True) -> Redis:
    return Redis.from_url(
        settings.valkey_url.get_secret_value(),
        decode_responses=decode_responses,
        socket_connect_timeout=5,
        socket_keepalive=True,
        health_check_interval=30,
        client_name='waystation',
    )
