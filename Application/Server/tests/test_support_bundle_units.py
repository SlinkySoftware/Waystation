"""Support bundle redaction and packaging (T-O10): no secret may reach the zip."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr
from ws_testing import make_repo, make_settings

from waystation_server.api import support
from waystation_server.repos.model import RepoType

PLANTED = 'Planted-S3cret-value'
DER = b'not really a certificate, but DER bytes all the same'
PEM = '-----BEGIN CERTIFICATE-----\n' + base64.encodebytes(DER).decode() + '-----END CERTIFICATE-----\n'
PRIVATE = '-----BEGIN ' + 'RSA PRIVATE KEY-----\nMIIabc\n-----END ' + 'RSA PRIVATE KEY-----'


def test_secret_keys_are_redacted_and_facts_about_them_kept() -> None:
    doc = {
        'password': PLANTED,
        'bind_password_enc': 'ENC' + PLANTED,
        'client_secret': PLANTED,
        'api_key': PLANTED,
        'access_token': PLANTED,
        'secret_set': True,
        'token_failure_limit': 60,
        'allow_private': True,
        'empty_password': '',
        'nested': [{'authorization': PLANTED, 'name': 'kept'}],
        'secretly': SecretStr(PLANTED),
    }
    out = support.scrub(doc)
    assert PLANTED not in json.dumps(out)
    assert out['password'] == support.REDACTED
    assert out['bind_password_set'] is True and 'bind_password_enc' not in out
    assert out['secret_set'] is True
    assert out['token_failure_limit'] == 60
    assert out['allow_private'] is True
    assert out['empty_password'] is None
    assert out['nested'] == [{'authorization': support.REDACTED, 'name': 'kept'}]
    assert out['secretly'] == support.REDACTED


def test_strings_lose_url_credentials_and_private_keys() -> None:
    out = support.scrub({'upstream_url': f'https://bob:{PLANTED}@registry.example.com/v2/', 'note': PRIVATE})
    assert out['upstream_url'] == 'https://***@registry.example.com/v2/'
    assert out['note'] == support.REDACTED


def test_values_are_bounded() -> None:
    out = support.scrub({'long': 'x' * 10_000, 'many': list(range(5000))})
    assert len(out['long']) == support.MAX_STRING
    assert len(out['many']) == support.MAX_ITEMS + 1
    deep: dict[str, object] = {}
    node = deep
    for _ in range(30):
        node['d'] = {}
        node = node['d']  # type: ignore[assignment]
    assert '…' in json.dumps(support.scrub(deep), ensure_ascii=False)


def test_ca_bundles_become_fingerprints() -> None:
    out = support.scrub({'ca_pem': PEM + PEM.replace('-----END', 'garbage!\n-----END')})
    assert out['ca_pem'] == {'certificates': 2, 'sha256': [hashlib.sha256(DER).hexdigest(), 'unreadable']}


def test_uris_keep_hosts_but_not_credentials() -> None:
    assert (
        support.safe_uri(f'mongodb://waystation:{PLANTED}@db1.example.com:27017,db2.example.com/?replicaSet=rs0')
        == 'mongodb://<redacted>@db1.example.com:27017,db2.example.com/?replicaSet=rs0'
    )
    assert (
        support.safe_uri(f'redis://:{PLANTED}@valkey.example.com:6379/0')
        == 'redis://<redacted>@valkey.example.com:6379/0'
    )
    assert (
        support.safe_uri(f'mongodb://db.example.com?tlsCertificateKeyFilePassword={PLANTED}&tls=true')
        == 'mongodb://db.example.com?tlsCertificateKeyFilePassword=<redacted>&tls=true'
    )
    assert support.safe_uri('redis://valkey.example.com:6379/0') == 'redis://valkey.example.com:6379/0'
    assert support.safe_uri(PLANTED) == support.REDACTED  # not a URI at all: nothing of it is shown


def test_environment_settings_redact_every_secret(tmp_path: Path) -> None:
    (tmp_path / 'bootstrap').write_text(PLANTED)
    settings = make_settings(
        tmp_path,
        mongo_uri=SecretStr(f'mongodb://ws:{PLANTED}@db.example.com:27017/?replicaSet=rs0'),
        valkey_url=SecretStr(f'redis://:{PLANTED}@valkey.example.com:6379/0'),
        session_secret=SecretStr(PLANTED),
        bootstrap_admin_password=SecretStr(PLANTED),
        http_proxy=f'http://proxy:{PLANTED}@proxy.example.com:3128',
        instance_name='Lab mirror',
    )
    env = {'WAYSTATION_BOOTSTRAP_ADMIN_PASSWORD_FILE': str(tmp_path / 'bootstrap')}
    out = support.environment(settings, env)
    text = json.dumps(out)
    assert PLANTED not in text
    variables = out['variables']
    assert variables['WAYSTATION_MONGO_URI'] == 'mongodb://<redacted>@db.example.com:27017/?replicaSet=rs0'
    assert variables['WAYSTATION_VALKEY_URL'] == 'redis://<redacted>@valkey.example.com:6379/0'
    assert variables['WAYSTATION_SECRET_KEYS'] == support.REDACTED
    assert variables['WAYSTATION_SESSION_SECRET'] == support.REDACTED
    assert variables['WAYSTATION_HTTP_PROXY'] == 'http://***@proxy.example.com:3128'
    assert variables['WAYSTATION_INSTANCE_NAME'] == 'Lab mirror'
    assert variables['WAYSTATION_DATA_DIR'] == str(tmp_path / 'data')
    assert out['secrets_from_files'] == ['WAYSTATION_BOOTSTRAP_ADMIN_PASSWORD_FILE']
    assert settings.secret_keys[0].get_secret_value() not in text


def test_the_zip_holds_every_file_and_no_secret(tmp_path: Path) -> None:
    repo = make_repo(
        'npm-proxy',
        'npm',
        type=RepoType.PROXY,
        proxy={
            'upstream_url': f'https://ci:{PLANTED}@registry.example.com/',
            'auth': {'kind': 'basic', 'username': 'ci', 'secret_enc': 'ENC-' + PLANTED},
            'ca_pem': PEM,
        },
    )
    sections = {
        'repositories.json': {'items': [repo.model_dump(mode='python', by_alias=False)]},
        'settings.json': {'environment': support.environment(make_settings(tmp_path, session_secret=PLANTED), {})},
        'errors.json': {'items': [{'msg': 'oops', 'password': PLANTED}]},
    }
    data = support.build_zip(sections, {'generated_at': datetime(2026, 11, 22, tzinfo=UTC)}, 'readme')
    assert PLANTED.encode() not in data
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist() == ['README.txt', 'manifest.json', 'repositories.json', 'settings.json', 'errors.json']
        everything = b''.join(zf.read(name) for name in zf.namelist())
        manifest = json.loads(zf.read('manifest.json'))
        stored = json.loads(zf.read('repositories.json'))['items'][0]
    assert PLANTED.encode() not in everything
    assert manifest['generated_at'] == '2026-11-22T00:00:00+00:00'
    assert [f['name'] for f in manifest['files']] == ['repositories.json', 'settings.json', 'errors.json']
    assert stored['proxy']['auth'] == {'kind': 'basic', 'username': 'ci', 'secret_set': True}
    assert stored['proxy']['ca_pem']['certificates'] == 1


def test_an_oversized_section_is_replaced_by_a_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(support, 'MAX_FILE_BYTES', 100)
    note = json.loads(support._json({'items': ['x' * 50] * 10}))
    assert note['truncated'] is True and note['limit'] == 100


def test_bundle_file_names() -> None:
    assert support._slug('Lab mirror (prod)') == 'lab-mirror-prod'
    assert support._slug('***') == 'waystation'
