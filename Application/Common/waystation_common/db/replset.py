"""Single-node replica set initiation for the bundled MongoDB (SPECIFICATION §8.1, run by ``init``).

The replica set name and the member address come from ``WAYSTATION_MONGO_URI`` (``replicaSet`` option and the
first host), so the address stored in the replica set config is the one every client uses.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from pymongo import AsyncMongoClient
from pymongo.errors import OperationFailure, PyMongoError
from pymongo.uri_parser import parse_uri

from waystation_common.errors import ConfigError

log = logging.getLogger('waystation.db.replset')

NOT_YET_INITIALIZED = 94
ALREADY_INITIALIZED = 23


def _member(uri: str) -> tuple[str, str, int, dict[str, Any]]:
    """(replica set name, host, port, auth kwargs) from a plain ``mongodb://`` URI."""
    if not uri.startswith('mongodb://'):
        raise ConfigError('replica set initiation needs a mongodb:// URI (not mongodb+srv://)')
    parsed = parse_uri(uri)
    rs_name = parsed['options'].get('replicaSet')
    if not rs_name:
        raise ConfigError('WAYSTATION_MONGO_URI has no replicaSet option')
    host, port = parsed['nodelist'][0]
    auth: dict[str, Any] = {}
    if parsed['username']:
        auth = {'username': parsed['username'], 'password': parsed['password']}
        if 'authSource' in parsed['options']:
            auth['authSource'] = parsed['options']['authSource']
    return str(rs_name), host, port, auth


async def ensure_replica_set(uri: str, *, timeout_s: float = 60.0) -> bool:
    """Initiate the replica set if needed and wait for a writable primary. True when this call initiated it."""
    rs_name, host, port, auth = _member(uri)
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
        host, port, directConnection=True, serverSelectionTimeoutMS=int(timeout_s * 1000), **auth
    )
    initiated = False
    try:
        try:
            await client.admin.command('replSetGetStatus')
        except OperationFailure as exc:
            if exc.code != NOT_YET_INITIALIZED:
                raise
            config = {'_id': rs_name, 'members': [{'_id': 0, 'host': f'{host}:{port}'}]}
            try:
                await client.admin.command('replSetInitiate', config)
                initiated = True
                log.info('replica set initiated', extra={'replica_set': rs_name, 'member': f'{host}:{port}'})
            except OperationFailure as race:
                if race.code != ALREADY_INITIALIZED:
                    raise
        await _wait_primary(client, timeout_s)
    finally:
        await client.close()
    return initiated


async def _wait_primary(client: AsyncMongoClient[dict[str, Any]], timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            hello = await client.admin.command('hello')
            if hello.get('isWritablePrimary'):
                return
        except PyMongoError:  # pragma: no cover - transient while the member elects itself
            pass
        if time.monotonic() > deadline:
            raise TimeoutError('MongoDB did not become primary in time')
        await asyncio.sleep(0.5)
