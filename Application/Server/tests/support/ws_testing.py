"""Helpers shared by the server tests (on ``sys.path`` via pytest's ``pythonpath``)."""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from starlette.responses import Response

from waystation_common.db.collections import REPOSITORIES
from waystation_common.settings import Settings
from waystation_server.app import create_app
from waystation_server.formats.base import FormatRequest
from waystation_server.proxy.engine import AssetSpec
from waystation_server.repos.catalog import RepoCatalog
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.services import Services

TEST_KEY = base64.b64encode(b'k' * 32).decode()
NOW = datetime(2026, 11, 8, tzinfo=UTC)
BASE_URL = 'https://registry.example.com'


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        'data_dir': tmp_path / 'data',
        'min_free_gb': 0,
        'secret_keys': TEST_KEY,
        'base_url': BASE_URL,
    }
    values.update(overrides)
    (tmp_path / 'data').mkdir(exist_ok=True)
    return Settings(**values)


def asgi_client(services: Services, *, client: tuple[str, int] = ('127.0.0.1', 50000)) -> httpx.AsyncClient:
    app = create_app(services.settings, services=services)
    transport = httpx.ASGITransport(app=app, client=client)
    return httpx.AsyncClient(transport=transport, base_url=BASE_URL)


def make_repo(name: str, fmt: str = 'raw', **fields: Any) -> RepoConfig:
    doc: dict[str, Any] = {
        '_id': f'id-{name}',
        'name': name,
        'format': fmt,
        'type': RepoType.PROXY,
        'proxy': {'upstream_url': 'https://upstream.example.com/'},
        'created_at': NOW,
        'updated_at': NOW,
    }
    doc.update(fields)
    return RepoConfig.model_validate(doc)


class FilesPlugin:
    """A minimal raw-like plugin on the proxy engine: the request path is the asset; ``make_spec`` shapes it."""

    def __init__(self, services: Services) -> None:
        descriptor = services.formats.descriptor('raw')
        assert descriptor is not None
        self.descriptor = descriptor
        self.make_spec: Callable[[str], AssetSpec] = lambda path: AssetSpec(path=path)

    async def handle(self, req: FormatRequest) -> Response:
        return await req.proxy.serve(req, self.make_spec(req.path))


async def add_repo(services: Services, url: str, *, name: str = 'files', repo_type: str = 'proxy', **proxy: Any) -> str:
    """Insert a raw repository straight into MongoDB (tests) and refresh the catalog."""
    repo_id = f'repo-{name}'
    now = datetime.now(UTC)
    await services.db[REPOSITORIES].insert_one(
        {
            '_id': repo_id,
            'name': name,
            'format': 'raw',
            'type': repo_type,
            'online': True,
            'proxy': {'upstream_url': url, **proxy} if repo_type == 'proxy' else None,
            'format_options': {},
            'created_at': now,
            'updated_at': now,
        }
    )
    services.catalog.invalidate()
    return repo_id


def static_catalog(repos: list[RepoConfig]) -> RepoCatalog:
    async def load() -> list[RepoConfig]:
        return list(repos)

    return RepoCatalog(load, ttl_s=0)
