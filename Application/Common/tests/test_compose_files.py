"""The compose files pass the same settings (SPECIFICATION §8.3): compose.yaml and the standalone server and worker
files share one ``x-app-env`` list, and it names every setting (secrets also as ``_FILE``)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from waystation_common.settings import ENV_PREFIX, FILE_SUFFIX, Settings, is_secret

COMPOSE_DIR = Path(__file__).resolve().parents[3] / 'Build' / 'Containers'
APP_ENV_FILES = ['compose.yaml', 'compose.standalone.server.yaml', 'compose.standalone.worker.yaml']
# Set per container rather than passed through: init only, or fixed by the image.
NOT_PASSED_THROUGH = {'WAYSTATION_MONGO_REPLSET_INIT'}


def _load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((COMPOSE_DIR / name).read_text())
    assert isinstance(data, dict)
    return data


@pytest.mark.parametrize('name', APP_ENV_FILES[1:])
def test_standalone_app_env_matches_compose_yaml(name: str) -> None:
    assert _load(name)['x-app-env'] == _load('compose.yaml')['x-app-env']


def test_app_env_names_every_setting() -> None:
    expected: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        env = f'{ENV_PREFIX}{field_name.upper()}'
        expected.add(env)
        if is_secret(field):
            expected.add(env + FILE_SUFFIX)
    missing = expected - set(_load('compose.yaml')['x-app-env']) - NOT_PASSED_THROUGH
    assert not missing, f'compose.yaml x-app-env does not pass through: {sorted(missing)}'


@pytest.mark.parametrize(
    'name', ['compose.yaml', *(f'compose.standalone.{r}.yaml' for r in ('web', 'server', 'worker'))]
)
def test_hardening_and_unique_aliases(name: str) -> None:
    for service_name, service in _load(name)['services'].items():
        assert service['cap_drop'] == ['ALL'], service_name
        assert service['security_opt'] == ['no-new-privileges:true'], service_name
        assert service['container_name'].startswith('waystation-'), service_name
        for network in service.get('networks', {}).values():
            assert all(alias.startswith('waystation-') for alias in network['aliases']), service_name
