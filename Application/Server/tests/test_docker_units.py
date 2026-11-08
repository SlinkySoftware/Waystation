"""Docker / OCI protocol helpers and upstream token cache (T-P2-DOCKERP), no I/O."""

import hashlib

import pytest

from waystation_common.errors import DigestMismatch, UpstreamError
from waystation_server.formats.docker import protocol as p
from waystation_server.formats.docker.upstream_auth import Realm, TokenCache, challenge_of, origin_of, parse_challenge

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
