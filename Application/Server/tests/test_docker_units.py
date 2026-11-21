"""Docker / OCI protocol helpers and upstream token cache (T-P2-DOCKERP), no I/O."""

import hashlib
import json

import pytest

from waystation_common.crypto import Keyring
from waystation_common.errors import DigestMismatch, UpstreamError
from waystation_server.formats.docker import protocol as p
from waystation_server.formats.docker.upstream_auth import (
    Realm,
    SharedTokens,
    TokenCache,
    challenge_of,
    origin_of,
    parse_challenge,
)

DIGEST = 'sha256:' + 'a' * 64


@pytest.mark.parametrize(
    ('path', 'expected'),
    [
        ('', p.Operation('base')),
        ('library/nginx/manifests/latest', p.Operation('manifest', 'library/nginx', 'latest')),
        (f'nginx/manifests/{DIGEST}', p.Operation('manifest', 'nginx', DIGEST)),
        (f'a/b/c/blobs/{DIGEST}', p.Operation('blob', 'a/b/c', DIGEST)),
        ('org/app/tags/list', p.Operation('tags', 'org/app')),
        (f'org/app/referrers/{DIGEST}', p.Operation('referrers', 'org/app', DIGEST)),
        # an image may be called "manifests" or "blobs": the last operation segment wins
        ('blobs/manifests/manifests/v1', p.Operation('manifest', 'blobs/manifests', 'v1')),
        ('ghcr.io/owner/app/manifests/1.2.3', p.Operation('manifest', 'ghcr.io/owner/app', '1.2.3')),
    ],
)
def test_parse(path: str, expected: p.Operation) -> None:
    assert p.parse(path) == expected


@pytest.mark.parametrize(
    ('path', 'code'),
    [
        ('Library/nginx/manifests/latest', 'NAME_INVALID'),
        ('nginx/manifests/-bad', 'MANIFEST_INVALID'),
        ('nginx/blobs/latest', 'DIGEST_INVALID'),
        ('nginx/blobs/sha256:abc', 'DIGEST_INVALID'),
        ('nginx', 'NAME_UNKNOWN'),
        ('nginx/manifests', 'NAME_UNKNOWN'),
        ('/manifests/x', 'NAME_INVALID'),
    ],
)
def test_parse_rejects(path: str, code: str) -> None:
    with pytest.raises(p.RequestRejected) as info:
        p.parse(path)
    assert info.value.code == code


def test_by_digest_and_library_prefix() -> None:
    assert p.Operation('manifest', 'x', DIGEST).by_digest
    assert not p.Operation('manifest', 'x', 'latest').by_digest
    assert p.upstream_name('nginx', docker_hub=True) == 'library/nginx'
    assert p.upstream_name('bitnami/nginx', docker_hub=True) == 'bitnami/nginx'
    assert p.upstream_name('nginx', docker_hub=False) == 'nginx'


def test_accepted_types() -> None:
    assert p.accepted_types([]) == p.MANIFEST_TYPES
    assert p.accepted_types(['*/*']) == p.MANIFEST_TYPES
    assert p.accepted_types([f'{p.DOCKER_MANIFEST}; q=0.5', p.OCI_INDEX]) == (p.OCI_INDEX, p.DOCKER_MANIFEST)
    assert p.accepted_types(['application/vnd.docker.distribution.manifest.v1+prettyjws']) == p.MANIFEST_TYPES
    assert p.accept_variant(p.MANIFEST_TYPES) == ''
    assert p.accept_variant((p.OCI_MANIFEST, p.DOCKER_MANIFEST)) == '@accept=om+dm'


def test_safe_content_type() -> None:
    assert p.safe_content_type(p.OCI_INDEX) == p.OCI_INDEX
    assert p.safe_content_type('application/json; charset=utf-8') == 'application/json'
    assert p.safe_content_type('text/html') == p.OCTET
    assert p.safe_content_type(None) == p.OCTET


def test_check_manifest() -> None:
    body = b'{"schemaVersion": 2}'
    good = 'sha256:' + hashlib.sha256(body).hexdigest()
    p.check_manifest(body, p.OCI_MANIFEST, good)
    p.check_manifest(body, p.OCI_MANIFEST, None)
    p.check_manifest(body, p.OCI_MANIFEST, 'sha512:' + hashlib.sha512(body).hexdigest())
    with pytest.raises(DigestMismatch):
        p.check_manifest(body, p.OCI_MANIFEST, DIGEST)
    with pytest.raises(UpstreamError):
        p.check_manifest(body, p.OCI_MANIFEST, 'md5:abc')
    with pytest.raises(UpstreamError):
        p.check_manifest(body, 'text/html', good)
    with pytest.raises(UpstreamError):
        p.check_manifest(b'<html>', p.OCI_MANIFEST, None)
    with pytest.raises(UpstreamError):
        p.check_manifest(b'[]', p.OCI_MANIFEST, None)


def test_check_tags() -> None:
    p.check_tags(b'{"name": "x", "tags": ["a"]}')
    p.check_tags(b'{"name": "x", "tags": null}')
    for bad in (b'nope', b'[]', b'{"tags": "a"}'):
        with pytest.raises(UpstreamError):
            p.check_tags(bad)


def test_tags_query_and_next_link() -> None:
    assert p.tags_query({'n': '010', 'last': 'v1', 'x': 'y'}) == {'n': '10', 'last': 'v1'}
    for bad in ({'n': '-1'}, {'n': '100001'}, {'last': '../x'}):
        with pytest.raises(p.RequestRejected):
            p.tags_query(bad)
    link = '</v2/library/nginx/tags/list?last=b&n=2>; rel="next"'
    assert p.next_link(link, '/v2/hub/nginx/tags/list') == '</v2/hub/nginx/tags/list?n=2&last=b>; rel="next"'
    assert p.next_link(None, '/x') is None
    assert p.next_link('<http://evil.example.com/?n=2>; rel="next"', '/x') is None
    assert p.next_link('</x?last=<script>>; rel="next"', '/x') is None
    assert p.next_link('</x?last=a>; rel="prev"', '/x') is None


def test_rate_limit() -> None:
    limit = p.rate_limit({'ratelimit-limit': '100;w=21600', 'ratelimit-remaining': '7;w=21600'})
    assert limit == p.RateLimit(limit=100, remaining=7, window_s=21600)
    assert p.rate_limit({'ratelimit-limit': '100'}) is None
    assert p.rate_limit({'ratelimit-limit': 'x', 'ratelimit-remaining': '1'}) is None
    assert p.rate_limit({'ratelimit-limit': '5', 'ratelimit-remaining': '1'}) == p.RateLimit(5, 1, None)


def test_parse_challenge() -> None:
    scheme, params = parse_challenge(
        'Bearer realm="https://auth.example.com/token",service="registry.example.com",scope="repository:a/b:pull"'
    )
    assert scheme == 'bearer'
    assert params == {
        'realm': 'https://auth.example.com/token',
        'service': 'registry.example.com',
        'scope': 'repository:a/b:pull',
    }
    assert parse_challenge('Basic realm=Registry') == ('basic', {'realm': 'Registry'})
    assert parse_challenge('') == ('', {})
    assert challenge_of('Bearer realm="https://auth.example.com/token",service="x"') == Realm(
        'https://auth.example.com/token', 'x'
    )
    assert challenge_of('Bearer service="x"') == 'none'
    assert challenge_of('Basic realm=Registry') == 'basic'
    assert challenge_of('') == 'none'


def test_origin_of() -> None:
    assert origin_of('https://Registry.Example.com/v2/') == ('https', 'registry.example.com', 443)
    assert origin_of('http://registry.example.com:5000/') == ('http', 'registry.example.com', 5000)


def test_token_cache_expiry() -> None:
    now = [1000.0]
    cache = TokenCache(clock=lambda: now[0])
    cache.put_token('r', 's', 'tok', 300)
    assert cache.token('r', 's') == 'tok'
    assert cache.token('r', 'other') is None
    now[0] += 290
    assert cache.token('r', 's') is None  # expired early (margin)
    cache.put_token('r', 's', 'tok2', 1)
    assert cache.token('r', 's') == 'tok2'
    cache.forget_token('r', 's')
    assert cache.token('r', 's') is None


class FakeValkey:
    def __init__(self, *, broken: bool = False) -> None:
        self.data: dict[str, bytes] = {}
        self.broken = broken

    async def get(self, key: str) -> bytes | None:
        if self.broken:
            raise ConnectionError('down')
        return self.data.get(key)

    async def set(self, key: str, value: str, px: int) -> None:
        if self.broken:
            raise ConnectionError('down')
        self.data[key] = value.encode()

    async def delete(self, key: str) -> None:
        if self.broken:
            raise ConnectionError('down')
        self.data.pop(key, None)


async def test_shared_tokens_are_encrypted_and_expire() -> None:
    now = [1000.0]
    valkey = FakeValkey()
    shared = SharedTokens(valkey, Keyring([b'k' * 32]), clock=lambda: now[0])  # type: ignore[arg-type]
    await shared.put_token('repo:1', 'repository:a:pull', 'secret-token', 120)
    [(key, value)] = valkey.data.items()
    assert key.startswith('uptok:') and b'secret-token' not in value and 'repo' not in key
    assert await shared.token('repo:1', 'repository:a:pull') == ('secret-token', 120)
    assert await shared.token('repo:2', 'repository:a:pull') is None  # another repository or configuration
    other_keys = SharedTokens(valkey, Keyring([b'o' * 32]))  # type: ignore[arg-type]
    assert await other_keys.token('repo:1', 'repository:a:pull') is None  # unreadable: never an error
    now[0] += 119.5
    assert await shared.token('repo:1', 'repository:a:pull') is None  # less than a second left
    await shared.forget_token('repo:1', 'repository:a:pull')
    assert valkey.data == {}
    without_keys = SharedTokens(valkey, None)  # type: ignore[arg-type]
    await without_keys.put_token('repo:1', 'repository:a:pull', 'secret-token', 120)
    assert valkey.data == {}  # never stored in clear
    assert await without_keys.token('repo:1', 'repository:a:pull') is None


async def test_shared_realms_and_valkey_failures() -> None:
    valkey = FakeValkey()
    shared = SharedTokens(valkey, Keyring([b'k' * 32]))  # type: ignore[arg-type]
    assert await shared.realm('repo:1') is None
    await shared.put_realm('repo:1', Realm('https://auth.example.com/token', 'registry.example.com'))
    assert await shared.realm('repo:1') == Realm('https://auth.example.com/token', 'registry.example.com')
    await shared.put_realm('repo:2', 'basic')
    assert await shared.realm('repo:2') == 'basic'
    valkey.data[next(iter(valkey.data))] = b'not json'  # repo:1's, stored first
    assert await shared.realm('repo:1') is None
    down = SharedTokens(FakeValkey(broken=True), Keyring([b'k' * 32]))  # type: ignore[arg-type]
    await down.put_token('repo:1', 's', 'tok', 60)
    await down.put_realm('repo:1', 'none')
    await down.forget_token('repo:1', 's')
    assert await down.token('repo:1', 's') is None
    assert await down.realm('repo:1') is None


def test_referrers_tag_schema() -> None:
    assert p.referrers_tag(DIGEST) == 'sha256-' + 'a' * 64
    assert p.referrers_tag('sha512:' + 'b' * 128) == 'sha512-' + 'b' * 64  # truncated to a valid tag
    assert p.TAG_RE.fullmatch(p.referrers_tag('sha512:' + 'b' * 128))


def test_referrer_entries_keep_only_well_formed_descriptors() -> None:
    good = {'mediaType': p.OCI_MANIFEST, 'digest': DIGEST, 'size': 10, 'artifactType': 'application/x.sig'}
    doc = {
        'schemaVersion': 2,
        'mediaType': p.OCI_INDEX,
        'manifests': [
            {**good, 'urls': ['http://198.51.100.7/'], 'annotations': {'a': 'b', 'n': 1}},
            good,  # duplicate
            {'mediaType': p.OCI_MANIFEST, 'digest': 'sha256:short', 'size': 1},
            {'mediaType': 'not a type', 'digest': 'sha256:' + 'c' * 64, 'size': 1},
            {'mediaType': p.OCI_MANIFEST, 'digest': 'sha256:' + 'd' * 64, 'size': True},
            {'mediaType': p.OCI_MANIFEST, 'digest': 'sha256:' + 'e' * 64, 'size': 3, 'artifactType': 5},
            'junk',
        ],
    }
    assert p.referrer_entries(json.dumps(doc).encode()) == [
        {**good, 'annotations': {'a': 'b'}},
        {'mediaType': p.OCI_MANIFEST, 'digest': 'sha256:' + 'e' * 64, 'size': 3},
    ]
    assert p.referrer_entries(b'nope') == []
    assert p.referrer_entries(b'[]') == []
    p.check_referrers(json.dumps(doc).encode())
    p.check_referrers(b'{"manifests": []}')
    for bad in (b'nope', b'[]', b'{"manifests": {}}', json.dumps({'mediaType': p.OCI_MANIFEST}).encode()):
        with pytest.raises(UpstreamError):
            p.check_referrers(bad)
