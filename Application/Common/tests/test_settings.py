from pathlib import Path

import pytest

from waystation_common import settings as settings_mod
from waystation_common.errors import ConfigError
from waystation_common.settings import LogFormat, Settings, get_settings, main, render_env_example

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in list(os.environ):
        if name.startswith('WAYSTATION_'):
            monkeypatch.delenv(name)


def test_defaults() -> None:
    s = Settings()
    assert s.base_url == 'http://localhost:8080'
    assert s.log_format is LogFormat.JSON
    assert s.secret_keys == []
    assert s.worker_queues == ['index', 'maintenance', 'stats', 'prefetch', 'import']
    assert s.max_upload_bytes == 64 * 1024**3
    assert s.min_free_bytes == 5 * 1024**3


def test_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('WAYSTATION_BASE_URL', 'https://registry.example.com/')
    monkeypatch.setenv('WAYSTATION_SECRET_KEYS', 'k1, k2,,')
    monkeypatch.setenv('WAYSTATION_TRUSTED_PROXIES', '192.0.2.0/24,198.51.100.7/32')
    monkeypatch.setenv('WAYSTATION_TRUST_FORWARDED', 'true')
    monkeypatch.setenv('WAYSTATION_LOG_FORMAT', 'console')
    s = Settings()
    assert s.base_url == 'https://registry.example.com'
    assert [k.get_secret_value() for k in s.secret_keys] == ['k1', 'k2']
    assert s.trusted_proxies == ['192.0.2.0/24', '198.51.100.7/32']
    assert s.trust_forwarded is True
    assert s.log_format is LogFormat.CONSOLE
    assert 'k1' not in repr(s)


def test_secret_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    keys = tmp_path / 'keys'
    keys.write_text('a\nb\n')
    uri = tmp_path / 'uri'
    uri.write_text('mongodb://user:pw@db.example.com:27017/\n')
    monkeypatch.setenv('WAYSTATION_SECRET_KEYS_FILE', str(keys))
    monkeypatch.setenv('WAYSTATION_MONGO_URI_FILE', str(uri))
    s = Settings()
    assert [k.get_secret_value() for k in s.secret_keys] == ['a', 'b']
    assert s.mongo_uri.get_secret_value() == 'mongodb://user:pw@db.example.com:27017/'


def test_secret_file_and_value_conflict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / 's'
    f.write_text('x')
    monkeypatch.setenv('WAYSTATION_SESSION_SECRET', 'y')
    monkeypatch.setenv('WAYSTATION_SESSION_SECRET_FILE', str(f))
    with pytest.raises(ConfigError, match='not both'):
        Settings()


def test_secret_file_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv('WAYSTATION_SESSION_SECRET_FILE', str(tmp_path / 'missing'))
    with pytest.raises(ConfigError, match='cannot read'):
        Settings()


def test_file_suffix_ignored_for_non_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / 'v'
    f.write_text('other')
    monkeypatch.setenv('WAYSTATION_MONGO_DB_FILE', str(f))
    assert Settings().mongo_db == 'waystation'


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()
    get_settings.cache_clear()


def test_env_example_is_current() -> None:
    """`.env.example` must equal the generated output (run `make env-example`)."""
    assert (REPO_ROOT / '.env.example').read_text() == render_env_example()


def test_env_example_contents() -> None:
    text = render_env_example()
    assert text == render_env_example()  # deterministic
    assert 'WAYSTATION_BASE_URL=https://registry.example.com\n' in text
    assert 'WAYSTATION_SECRET_KEYS=\n' in text
    assert 'WAYSTATION_TRUST_FORWARDED=false\n' in text
    assert 'WAYSTATION_LOG_FORMAT=json\n' in text
    assert 'WAYSTATION_HTTP_PROXY=\n' in text
    assert 'WAYSTATION_METRICS_ALLOW_CIDRS=127.0.0.0/8,::1/128\n' in text
    for name in Settings.model_fields:
        assert f'WAYSTATION_{name.upper()}=' in text


def test_main(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(['prog', 'env-example']) == 0
    assert capsys.readouterr().out == render_env_example()
    assert main(['prog']) == 2
    assert 'usage' in capsys.readouterr().err


def test_is_secret_marker() -> None:
    fields = Settings.model_fields
    assert settings_mod.is_secret(fields['secret_keys'])
    assert not settings_mod.is_secret(fields['base_url'])
