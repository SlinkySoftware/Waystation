"""Async MongoDB client factory (PyMongo ``AsyncMongoClient``; ARCHITECTURE §2).

One client per process, created at start-up and closed at shutdown::

    client = create_client(settings)
    db = database(client, settings)
    ...
    await client.close()

Creating the client does not connect; the first operation does.
"""

from __future__ import annotations

from typing import Any

from pymongo import AsyncMongoClient
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase

from waystation_common.settings import Settings

Document = dict[str, Any]
Client = AsyncMongoClient[Document]
Database = AsyncDatabase[Document]
Session = AsyncClientSession  # a transaction's session, passed through to writes that must join it


def create_client(settings: Settings, *, app_name: str = 'waystation', **overrides: Any) -> Client:
    options: dict[str, Any] = {
        'appname': app_name,
        'tz_aware': True,
        'serverSelectionTimeoutMS': 10_000,
        'connectTimeoutMS': 10_000,
        'retryWrites': True,
    }
    options.update(overrides)
    return AsyncMongoClient(settings.mongo_uri.get_secret_value(), **options)


def database(client: Client, settings: Settings) -> Database:
    return client[settings.mongo_db]
