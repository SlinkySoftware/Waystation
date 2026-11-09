"""``GET /system`` helpers (T-U08): volume figures and how a failing dependency is reported."""

from pathlib import Path

import pytest

from waystation_common.settings import Settings
from waystation_server import instance
from waystation_server.api import settings as settings_api
from waystation_server.db import settings as settings_db


def test_volume_of_an_existing_directory(tmp_path: Path) -> None:
    figures = settings_api._volume(tmp_path)
    assert figures is not None
    total, used, free = figures
    assert total > 0
    assert 0 <= used <= total
    assert 0 <= free <= total


def test_volume_of_a_missing_directory(tmp_path: Path) -> None:
    assert settings_api._volume(tmp_path / 'missing') is None


async def test_instance_settings_fall_back_when_mongodb_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def failing(db: object, section: str) -> dict[str, object] | None:
        nonlocal calls
        calls += 1
        raise ConnectionError('down')

    monkeypatch.setattr(settings_db, 'get', failing)
    store = instance.InstanceSettings(object(), Settings(instance_name='Lab mirror'))  # type: ignore[arg-type]
    assert (await store.general()).instance_name == 'Lab mirror'
    assert (await store.general()).instance_name == 'Lab mirror'
    assert calls == 1  # cached for TTL_S, not retried on every request


def test_errors_name_only_the_class() -> None:
    assert settings_api._error(TimeoutError()) == 'timeout'
    assert settings_api._error(ConnectionRefusedError('secret host 192.0.2.1')) == 'ConnectionRefusedError'
