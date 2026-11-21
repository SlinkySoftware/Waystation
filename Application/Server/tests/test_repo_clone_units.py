"""Repository clone (T-S05): the create request built from a stored configuration."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from waystation_common.crypto import Keyring
from waystation_server.repos.model import (
    AuthKind,
    RepoConfig,
    RepoType,
    StoredGroup,
    StoredHosted,
    StoredProxy,
    StoredUpstreamAuth,
    WritePolicy,
    secret_aad,
)
from waystation_server.repos.service import clone_body
from waystation_server.services import Services

NOW = datetime(2026, 11, 21, tzinfo=UTC)
KEYRING = Keyring([b'k' * 32])


def services(keyring: Keyring | None = KEYRING) -> Services:
    return cast(Services, SimpleNamespace(keyring=keyring))


def repo(**fields: Any) -> RepoConfig:
    base: dict[str, Any] = {
        '_id': 'src-1',
        'name': 'source',
        'format': 'docker',
        'type': RepoType.PROXY,
        'description': 'the source',
        'created_at': NOW,
        'updated_at': NOW,
        'managed_by': 'provisioning',
        'cleanup_policy_ids': ('p1',),
    }
    base.update(fields)
    return RepoConfig.model_validate(base)


def test_proxy_clone_carries_the_decrypted_secret_and_drops_unique_options() -> None:
    enc = KEYRING.encrypt_str('hunter2', aad=secret_aad('src-1'))
    source = repo(
        proxy=StoredProxy(
            upstream_url='https://registry.example.com',
            auth=StoredUpstreamAuth(kind=AuthKind.BEARER, secret_enc=enc),
            negative_ttl_s=5,
        ),
        format_options={'aliases': ['registry.example.com'], 'default_repo': True, 'docker_hub': False},
    )
    body = clone_body(services(), source, 'copy')
    assert body.name == 'copy'
    assert body.type is RepoType.PROXY
    assert body.online is True
    assert body.description == 'the source'
    assert body.proxy is not None
    assert body.proxy.auth.kind is AuthKind.BEARER
    assert body.proxy.auth.secret == 'hunter2'
    assert body.proxy.negative_ttl_s == 5
    assert body.format_options == {'docker_hub': False}
    assert 'hunter2' not in body.model_dump_json()  # write-only: never serialised


def test_secret_that_cannot_be_decrypted_is_left_out() -> None:
    enc = Keyring([b'x' * 32]).encrypt_str('hunter2', aad=secret_aad('src-1'))
    source = repo(
        proxy=StoredProxy(
            upstream_url='https://registry.example.com',
            auth=StoredUpstreamAuth(kind=AuthKind.BASIC, username='ci', secret_enc=enc),
        )
    )
    body = clone_body(services(), source, 'copy')
    assert body.proxy is not None and body.proxy.auth.secret is None
    assert clone_body(services(None), source, 'copy').proxy.auth.secret is None  # type: ignore[union-attr]


def test_hosted_and_group_clones_and_overrides() -> None:
    hosted = repo(
        type=RepoType.HOSTED,
        hosted=StoredHosted(write_policy=WritePolicy.READ_ONLY, immutable_tag_patterns=('v.*',)),
    )
    body = clone_body(services(), hosted, 'copy', description='', online=False)
    assert body.hosted is not None
    assert body.hosted.write_policy is WritePolicy.READ_ONLY
    assert body.hosted.immutable_tag_patterns == ['v.*']
    assert body.description == ''
    assert body.online is False
    assert body.proxy is None and body.group is None
    group = repo(type=RepoType.GROUP, group=StoredGroup(members=('aa', 'bb'), writable_member='bb'))
    body = clone_body(services(), group, 'copy')
    assert body.group is not None
    assert body.group.members == ['aa', 'bb']
    assert body.group.writable_member == 'bb'
    assert body.hosted is None
