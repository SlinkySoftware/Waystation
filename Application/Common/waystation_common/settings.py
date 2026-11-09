"""Process configuration from the environment (SPECIFICATION §9).

Every variable is ``WAYSTATION_<NAME>``. Every field marked as a secret also accepts ``WAYSTATION_<NAME>_FILE``,
the path of a file holding the value (Docker/compose secrets); setting both is a configuration error. Secrets are
``SecretStr`` so they never appear in ``repr``, logs or tracebacks.

``.env.example`` at the repository root is generated from this model (``make env-example``, i.e.
``python -m waystation_common.settings env-example``); a unit test fails when it is stale. Add new settings here,
with a ``description``, never by hand in ``.env.example``.

Settings are read once per process at start-up (``get_settings()``, synchronous by design: it runs before the event
loop serves requests).
"""

from __future__ import annotations

import os
import sys
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator
from pydantic.config import JsonDict
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource, SettingsConfigDict

from waystation_common.errors import ConfigError

ENV_PREFIX = 'WAYSTATION_'
FILE_SUFFIX = '_FILE'

# Marker in a field's json_schema_extra: the value is a secret and may be given as <NAME>_FILE.
_SECRET: JsonDict = {'secret': True}


class LogFormat(StrEnum):
    JSON = 'json'
    CONSOLE = 'console'


class CoalesceTail(StrEnum):
    AUTO = 'auto'
    LOCAL = 'local'
    WAIT = 'wait'


def _split_list(value: Any) -> Any:
    """Comma/newline separated string -> list of stripped, non-empty items. Lists pass through."""
    if isinstance(value, str):
        return [item.strip() for item in value.replace('\n', ',').split(',') if item.strip()]
    return value


CommaList = Annotated[list[str], NoDecode]
SecretList = Annotated[list[SecretStr], NoDecode]


def is_secret(field: FieldInfo) -> bool:
    extra = field.json_schema_extra
    return isinstance(extra, dict) and extra.get('secret') is True


class _SecretFileSource(PydanticBaseSettingsSource):
    """Reads ``WAYSTATION_<NAME>_FILE`` for secret fields."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:  # pragma: no cover
        raise NotImplementedError  # __call__ is overridden; the abstract hook is unused

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, field in self.settings_cls.model_fields.items():
            if not is_secret(field):
                continue
            env_name = f'{ENV_PREFIX}{name.upper()}'
            file_var = env_name + FILE_SUFFIX
            path = os.environ.get(file_var)
            if not path:
                continue
            if os.environ.get(env_name):
                raise ConfigError(f'set either {env_name} or {file_var}, not both')
            try:
                content = Path(path).read_text(encoding='utf-8')
            except OSError as exc:
                raise ConfigError(f'{file_var}: cannot read {path}: {exc.strerror}') from None
            values[name] = content.rstrip('\r\n')
        return values


class Settings(BaseSettings):
    """All process settings. Field order is the order in ``.env.example``."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, case_sensitive=False, extra='ignore', frozen=True, env_ignore_empty=True
    )

    # --- general
    base_url: str = Field(
        'http://localhost:8080',
        description='Public base URL clients use (scheme, host, optional port; no trailing slash).',
        json_schema_extra={'example': 'https://registry.example.com'},
    )
    instance_name: str = Field('Waystation', description='Name shown in the UI top bar and on the sign-in page.')
    log_level: str = Field('INFO', description='Log level: DEBUG, INFO, WARNING, ERROR.')
    log_format: LogFormat = Field(LogFormat.JSON, description='Log format: json (default) or console.')

    # --- storage
    data_dir: Path = Field(
        Path('/var/lib/waystation'),
        description='Blob store root (blobs/, uploads/, keys/). Same path in every container.',
    )
    uid: int = Field(10001, description='UID owning the data dir (server and worker run as this user).')
    gid: int = Field(10001, description='Shared GID for server, worker and web (ARCHITECTURE §4.5).')
    max_upload_gb: float = Field(64.0, description='Largest single upload or cached file, in GiB.')
    min_free_gb: float = Field(5.0, description='Refuse new cache/upload writes below this much free space, in GiB.')
    coalesce_tail: CoalesceTail = Field(
        CoalesceTail.AUTO, description='Coalescing followers: auto, local (always tail) or wait (ARCHITECTURE §6.3).'
    )
    coalesce_lease_s: float = Field(
        15.0,
        gt=0.5,
        le=300,
        description='Coalescing lease, in seconds: a fill whose owner stops renewing it is taken over after this long.',
    )

    # --- MongoDB and Valkey (URIs may embed credentials, so they are secrets)
    mongo_uri: SecretStr = Field(
        SecretStr('mongodb://waystation-mongodb:27017/?replicaSet=rs0'),
        description='MongoDB connection URI.',
        json_schema_extra=_SECRET,
    )
    mongo_db: str = Field('waystation', description='MongoDB database name.')
    mongo_replset_init: bool = Field(
        default=False,
        description='init: initiate the single-node replica set named in MONGO_URI if needed (bundled MongoDB).',
    )
    valkey_url: SecretStr = Field(
        SecretStr('redis://waystation-valkey:6379/0'), description='Valkey connection URL.', json_schema_extra=_SECRET
    )

    # --- secrets
    secret_keys: SecretList = Field(
        default_factory=list,
        description=(
            'Comma-separated base64 AES-256 keys; the first encrypts, all decrypt. '
            'Generate with: head -c 32 /dev/urandom | base64'
        ),
        json_schema_extra=_SECRET,
    )
    session_secret: SecretStr | None = Field(None, description='Session signing secret.', json_schema_extra=_SECRET)
    bootstrap_admin_password: SecretStr | None = Field(
        None,
        description='First admin password (prefer the _FILE form); unset = a one-time password is printed by init.',
        json_schema_extra=_SECRET,
    )

    # --- sign-in and sessions (SPECIFICATION §4.9)
    session_idle_minutes: int = Field(480, ge=5, description='Sign out a UI session after this long without use.')
    session_max_hours: int = Field(168, ge=1, description='Absolute lifetime of a UI session, in hours.')
    login_rate_limit: int = Field(
        20, ge=1, description='Sign-in attempts allowed per client address per 5 minutes (then 429).'
    )
    login_lockout_failures: int = Field(10, ge=1, description='Consecutive failed sign-ins that lock a local account.')
    login_lockout_minutes: int = Field(15, ge=1, description='How long a locked local account stays locked.')
    token_failure_limit: int = Field(
        60, ge=1, description='Rejected API tokens allowed per client address per 5 minutes (then 429).'
    )
    audit_retention_days: int = Field(
        400, ge=1, le=36500, description='Days an audit record is kept (applies to records written from then on).'
    )

    # --- network and proxies
    trust_forwarded: bool = Field(default=False, description='Trust X-Forwarded-* from TRUSTED_PROXIES.')
    trusted_proxies: CommaList = Field(
        default_factory=list, description='Comma-separated CIDRs of trusted reverse proxies.'
    )
    metrics_allow_cidrs: CommaList = Field(
        default_factory=lambda: ['127.0.0.0/8', '::1/128'],
        description='Comma-separated CIDRs allowed to scrape /metrics without a token.',
    )
    ssrf_allow_cidrs: CommaList = Field(
        default_factory=list, description='Private CIDRs upstreams may resolve to (SSRF guard exceptions).'
    )
    http_proxy: str | None = Field(None, description='Outbound HTTP proxy for upstream fetches.')
    no_proxy: str | None = Field(None, description='Hosts that bypass HTTP_PROXY.')
    extra_ca_file: Path | None = Field(None, description='Extra CA bundle (PEM) trusted for upstream TLS.')

    # --- processes
    server_workers: int = Field(1, ge=1, description='Server processes per container.')
    worker_queues: CommaList = Field(
        default_factory=lambda: ['index', 'maintenance', 'stats', 'prefetch', 'import'],
        description='Comma-separated job queues this worker consumes.',
    )
    worker_concurrency: int = Field(4, ge=1, description='Concurrent jobs per worker.')

    @field_validator('trusted_proxies', 'metrics_allow_cidrs', 'ssrf_allow_cidrs', 'worker_queues', mode='before')
    @classmethod
    def _split(cls, value: Any) -> Any:
        return _split_list(value)

    @field_validator('secret_keys', mode='before')
    @classmethod
    def _split_secret(cls, value: Any) -> Any:
        return _split_list(value)

    @field_validator('base_url')
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip('/')

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_gb * 1024**3)

    @property
    def min_free_bytes(self) -> int:
        return int(self.min_free_gb * 1024**3)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, env_settings, _SecretFileSource(settings_cls)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process settings (read once)."""
    return Settings()


def _example_value(name: str, field: FieldInfo) -> str:
    extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
    if 'example' in extra:
        return str(extra['example'])
    if is_secret(field):
        if name in ('mongo_uri', 'valkey_url'):
            return str(field.get_default(call_default_factory=True).get_secret_value())
        return ''
    default = field.get_default(call_default_factory=True)
    if default is None:
        return ''
    if isinstance(default, list):
        return ','.join(str(item) for item in default)
    if isinstance(default, bool):
        return 'true' if default else 'false'
    if isinstance(default, StrEnum):
        return default.value
    return str(default)


def render_env_example() -> str:
    """The content of ``.env.example`` (deterministic)."""
    lines = [
        '# Waystation environment (example). Copy to an env file outside the repo and fill in.',
        '# GENERATED from waystation_common.settings by `make env-example`; do not edit by hand.',
        '# Every secret (marked [secret]) can instead be read from a file: set <NAME>_FILE to its path.',
        '# Never put real hostnames or secrets in this file.',
    ]
    for name, field in Settings.model_fields.items():
        env = f'{ENV_PREFIX}{name.upper()}'
        tag = ' [secret]' if is_secret(field) else ''
        lines.append('')
        lines.append(f'# {field.description}{tag}')
        lines.append(f'{env}={_example_value(name, field)}')
    return '\n'.join(lines) + '\n'


def main(argv: list[str]) -> int:
    if argv[1:] == ['env-example']:
        sys.stdout.write(render_env_example())
        return 0
    sys.stderr.write('usage: python -m waystation_common.settings env-example\n')
    return 2


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main(sys.argv))
