"""Provisioning file (T-O09): schema, safe YAML loading, secret references, plan rendering (no database)."""

import json
from pathlib import Path

import pytest

from waystation_server.provisioning import loader, runner
from waystation_server.provisioning.model import ProvisioningFile, SecretRef, json_schema
from waystation_server.provisioning.report import (
    ProvisioningChange,
    ProvisioningItem,
    ProvisioningPlan,
    ProvisioningSummary,
)

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = ROOT / 'Documents/Operations/provisioning.schema.json'
EXAMPLE = ROOT / 'Documents/Operations/provisioning.example.yaml'
INLINE = 'hunter2-inline-secret-value'

MINIMAL = b"""
version: 1
repositories:
  - name: docker-hub
    format: docker
    type: proxy
    proxy:
      upstream_url: https://registry-1.docker.io
      auth: {kind: basic, username: puller, secret: {file: /run/secrets/hub}}
"""


def test_minimal_file_parses() -> None:
    document, problems = loader.parse(MINIMAL)
    assert problems == []
    assert document is not None
    repo = (document.repositories or [])[0]
    assert repo.online is True  # defaults like the API's, except online (true unless declared)
    assert repo.proxy is not None
    assert repo.proxy.auth.secret == SecretRef(file='/run/secrets/hub')
    assert document.roles is None  # an absent section is not managed
    assert document.prune.repositories is False


def test_inline_secret_is_refused_without_echoing_it() -> None:
    text = MINIMAL.replace(b'{file: /run/secrets/hub}', INLINE.encode())
    document, problems = loader.parse(text)
    assert document is None
    assert [p.field for p in problems] == ['repositories.0.proxy.auth.secret']
    assert 'inline secrets are not allowed' in problems[0].message
    assert INLINE not in json.dumps([p.__dict__ for p in problems])


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        (b'version: 1\nroles: &r []\ngroups: *r\n', 'aliases are not allowed'),
        (b'version: 1\nversion: 1\n', "duplicate key 'version'"),
        (b'- 1\n- 2\n', 'must be a YAML mapping'),
        (b'version: [1\n', 'line 2'),
        (b'\xff\xfe', 'not UTF-8'),
        (b'version: 2\n', 'Input should be 1'),
        (b'version: 1\nunknown: true\n', 'Extra inputs are not permitted'),
    ],
)
def test_bad_files_are_reported(text: bytes, expected: str) -> None:
    document, problems = loader.parse(text)
    assert document is None
    assert expected in ' '.join(f'{p.field}: {p.message}' for p in problems)


def test_parse_errors_never_quote_the_file() -> None:
    _, problems = loader.parse(f'version: 1\nroles: [{INLINE}: : :\n'.encode())
    assert problems and INLINE not in problems[0].message


def test_size_and_depth_are_bounded() -> None:
    _, problems = loader.parse(b'#' * (loader.MAX_FILE_BYTES + 1))
    assert problems[0].message.startswith('larger than')
    deep = b'version: 1\nsettings: ' + b'[' * 40 + b']' * 40 + b'\n'
    _, problems = loader.parse(deep)
    assert 'nested too deeply' in problems[0].message


def test_names_are_unique_per_section() -> None:
    text = b"""
version: 1
service_accounts: [{name: ci-a}, {name: ci-a}]
auth_providers:
  - {kind: oidc, name: SSO, oidc: {issuer: https://sso.example.com, client_id: w, client_auth: none}}
  - {kind: oidc, name: sso, oidc: {issuer: https://sso.example.com, client_id: w, client_auth: none}}
"""
    _, problems = loader.parse(text)
    assert {p.field for p in problems} == {'service_accounts', 'auth_providers'}


@pytest.mark.parametrize(
    'ref',
    [{}, {'file': '/a', 'env': 'B'}, {'file': 'relative/path'}, {'env': 'not a name'}],
)
def test_secret_refs_need_exactly_one_valid_source(ref: dict[str, str]) -> None:
    with pytest.raises(ValueError, match='exactly one|absolute|variable name'):
        SecretRef.model_validate(ref)


async def test_resolve_reads_files_and_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / 'secret'
    path.write_text('s3cret-value\n')
    assert await loader.resolve(SecretRef(file=str(path))) == 's3cret-value'
    monkeypatch.setenv('HUB_TOKEN', 'from-env')
    assert await loader.resolve(SecretRef(env='HUB_TOKEN')) == 'from-env'

    for ref, message in [
        (SecretRef(env='MISSING_VARIABLE'), 'env MISSING_VARIABLE is not set'),
        (SecretRef(env='WAYSTATION_SECRET_KEYS'), 'is a Waystation setting'),
        (SecretRef(env='WAYSTATION_MONGO_URI_FILE'), 'is a Waystation setting'),
        (SecretRef(file=str(tmp_path / 'nope')), 'cannot read it'),
    ]:
        with pytest.raises(loader.SecretError, match=message):
            await loader.resolve(ref)
    (tmp_path / 'empty').write_text('\n')
    with pytest.raises(loader.SecretError, match='is empty'):
        await loader.resolve(SecretRef(file=str(tmp_path / 'empty')))
    (tmp_path / 'big').write_bytes(b'x' * 70_000)
    with pytest.raises(loader.SecretError, match='larger than'):
        await loader.resolve(SecretRef(file=str(tmp_path / 'big')))


async def test_load_reports_an_unreadable_file(tmp_path: Path) -> None:
    loaded = await loader.load(tmp_path / 'missing.yaml')
    assert not loaded.valid
    assert loaded.sha256 is None
    assert loaded.errors[0].field == 'file'


def test_render_shows_secret_changes_only_as_redacted() -> None:
    plan = ProvisioningPlan(
        file='/etc/waystation/provisioning.yaml',
        sha256='ab',
        valid=True,
        applied=True,
        items=[
            ProvisioningItem(
                kind='repository',
                name='docker-hub',
                action='update',
                changes=[
                    ProvisioningChange(field='description', before='a', after='b'),
                    ProvisioningChange(field='proxy.auth.secret', before='***', after='***', redacted=True),
                ],
                outcome='applied',
            )
        ],
        summary=ProvisioningSummary(update=1),
    )
    text = runner.render(plan)
    assert "description: 'a' -> 'b'" in text
    assert 'proxy.auth.secret: (secret changed)' in text
    assert 'applied: 0 create, 1 update' in text


def test_committed_json_schema_is_current() -> None:
    """``make provision-schema`` regenerates it; editors validate files with it."""
    assert json.loads(SCHEMA.read_text()) == json_schema()


def test_documented_example_is_valid() -> None:
    document, problems = loader.parse(EXAMPLE.read_bytes())
    assert problems == []
    assert isinstance(document, ProvisioningFile)
    assert document.repositories and document.roles and document.auth_providers and document.webhooks
