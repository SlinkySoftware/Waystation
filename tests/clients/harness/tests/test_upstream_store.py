from __future__ import annotations

import json
from pathlib import Path

import pytest
from upstream_store import REDACTED, Exchange, FixtureStore, RequestKey, auth_kind, scrub_response, scrub_url


def test_scrub_url_redacts_signed_query_values_and_userinfo() -> None:
    url = 'https://user:pw@cdn.example.com/blob?X-Amz-Signature=abc&X-Amz-Expires=1&keep=1&token=t'
    scrubbed = scrub_url(url)
    assert scrubbed == (
        f'https://cdn.example.com/blob?X-Amz-Signature={REDACTED}&X-Amz-Expires={REDACTED}&keep=1&token={REDACTED}'
    )
    assert scrub_url(scrubbed) == scrubbed  # idempotent: replayed Locations map to the same key


def test_scrub_url_leaves_ordinary_queries_byte_identical() -> None:
    url = 'https://auth.docker.io/token?scope=repository:library/busybox:pull&service=registry.docker.io'
    assert scrub_url(url) == url


def test_auth_kind_keeps_only_the_scheme() -> None:
    assert auth_kind(None) == 'none'
    assert auth_kind('Bearer abc.def') == 'bearer'
    assert auth_kind('Basic Zm9vOmJhcg==') == 'basic'


def test_key_ignores_credentials_and_volatile_headers() -> None:
    a = RequestKey.build('GET', 'https://echo.example.com/x', {'Authorization': 'Bearer one', 'User-Agent': 'a'})
    b = RequestKey.build('get', 'https://echo.example.com/x', {'authorization': 'Bearer two', 'Cookie': 'c=1'})
    assert a.digest == b.digest


@pytest.mark.parametrize(
    ('headers', 'body'),
    [
        ({}, b''),  # anonymous vs bearer
        ({'Authorization': 'Bearer x', 'Accept': 'application/json'}, b''),
        ({'Authorization': 'Bearer x', 'Range': 'bytes=0-1'}, b''),
        ({'Authorization': 'Bearer x', 'If-None-Match': '"e"'}, b''),
        ({'Authorization': 'Bearer x'}, b'payload'),
    ],
)
def test_key_distinguishes_representations(headers: dict[str, str], body: bytes) -> None:
    base = RequestKey.build('GET', 'https://echo.example.com/x', {'Authorization': 'Bearer x'})
    other = RequestKey.build('GET', 'https://echo.example.com/x', headers, body)
    assert base.digest != other.digest


def test_scrub_response_drops_unlisted_headers_and_redacts_json_tokens() -> None:
    body = json.dumps({'token': 'secret-1', 'nested': {'access_token': 'secret-2'}, 'expires_in': 300}).encode()
    headers = [
        ('Content-Type', 'application/json'),
        ('Content-Length', str(len(body))),
        ('Set-Cookie', 'session=1'),
        ('X-Client-Ip', '192.0.2.10'),
        ('Docker-RateLimit-Source', '192.0.2.10'),
        ('Location', 'https://cdn.example.com/b?Signature=s'),
    ]
    out = scrub_response(200, headers, body)
    names = [k for k, _ in out.headers]
    assert names == ['content-type', 'content-length', 'location']
    assert dict(out.headers)['location'] == f'https://cdn.example.com/b?Signature={REDACTED}'
    doc = json.loads(out.body)
    assert doc == {'token': REDACTED, 'nested': {'access_token': REDACTED}, 'expires_in': 300}
    assert dict(out.headers)['content-length'] == str(len(out.body))
    assert b'secret' not in out.body


def test_scrub_response_keeps_bytes_when_nothing_to_redact() -> None:
    body = b'{ "name" : "is-number" }'
    assert scrub_response(200, [('content-type', 'application/json')], body).body == body
    assert scrub_response(200, [('content-type', 'application/json')], b'not json').body == b'not json'


def test_store_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    store = FixtureStore(tmp_path)
    key = RequestKey.build('GET', 'https://echo.example.com/blob/4', {'Accept': '*/*'})
    assert store.load(key) is None
    meta = store.save(key, Exchange(200, [('etag', '"x"')], b'\x00\x01\x02\x03'))
    assert meta.parent.name == 'echo.example.com'
    loaded = store.load(key)
    assert loaded is not None
    assert (loaded.status, loaded.headers, loaded.body) == (200, [('etag', '"x"')], b'\x00\x01\x02\x03')

    meta.with_suffix('.body').write_bytes(b'tampered')
    with pytest.raises(ValueError, match='sha256'):
        store.load(key)


def test_store_bodiless_exchange_removes_stale_body(tmp_path: Path) -> None:
    store = FixtureStore(tmp_path)
    key = RequestKey.build('HEAD', 'https://echo.example.com/x', {})
    store.save(key, Exchange(200, [], b'old'))
    meta = store.save(key, Exchange(304, [], b''))
    assert not meta.with_suffix('.body').exists()
    loaded = store.load(key)
    assert loaded is not None
    assert (loaded.status, loaded.body) == (304, b'')


def test_scrub_response_drops_configured_json_keys_at_any_depth() -> None:
    body = json.dumps(
        {'name': 'pkg', 'readme': 'see https://example.com', 'versions': {'1.0.0': {'_npmUser': {'email': 'a@b'}}}}
    ).encode()
    out = scrub_response(200, [('content-type', 'application/json')], body, frozenset({'readme', '_npmUser'}))
    assert json.loads(out.body) == {'name': 'pkg', 'versions': {'1.0.0': {}}}
