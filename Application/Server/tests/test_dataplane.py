from datetime import UTC, datetime

import pytest
from starlette.requests import Request

from waystation_server.dataplane.xaccel import ServedBlob, serve_blob

SHA = 'ab' * 32
BLOB = ServedBlob(
    sha256=SHA,
    size=1234,
    content_type='application/vnd.oci.image.manifest.v1+json',
    last_modified=datetime(2026, 11, 8, 10, 0, 0, tzinfo=UTC),
    headers={'Docker-Content-Digest': f'sha256:{SHA}'},
)


def request(method: str = 'GET', **headers: str) -> Request:
    raw = [(k.replace('_', '-').lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({'type': 'http', 'method': method, 'path': '/x', 'headers': raw, 'query_string': b''})


def test_get_redirects_to_nginx_with_an_empty_body() -> None:
    response = serve_blob(request(), BLOB, cache='HIT')
    assert response.status_code == 200
    assert response.body == b''
    assert response.headers['x-accel-redirect'] == f'/_blobs/sha256/ab/ab/{SHA}'
    assert response.headers['content-type'] == 'application/vnd.oci.image.manifest.v1+json'
    assert response.headers['x-ws-etag'] == f'"sha256:{SHA}"'
    assert response.headers['x-ws-digest'] == f'sha256:{SHA}'
    assert response.headers['x-ws-cache'] == 'HIT'
    assert response.headers['docker-content-digest'] == f'sha256:{SHA}'


def test_head_is_answered_by_the_server() -> None:
    response = serve_blob(request('HEAD'), BLOB)
    assert response.status_code == 200
    assert 'x-accel-redirect' not in response.headers
    assert response.headers['content-length'] == '1234'


def test_text_types_get_no_charset() -> None:
    blob = ServedBlob(sha256=SHA, size=1, content_type='text/plain')
    assert serve_blob(request(), blob).headers['content-type'] == 'text/plain'


@pytest.mark.parametrize(
    'if_none_match', [f'"sha256:{SHA}"', f'W/"sha256:{SHA}"', f'"other", "sha256:{SHA}"', '*', f'sha256:{SHA}']
)
def test_if_none_match(if_none_match: str) -> None:
    response = serve_blob(request(if_none_match=if_none_match), BLOB)
    assert response.status_code == 304
    assert response.headers['etag'] == f'"sha256:{SHA}"'
    assert response.headers['docker-content-digest'] == f'sha256:{SHA}'
    assert 'x-accel-redirect' not in response.headers


def test_if_none_match_mismatch_redirects() -> None:
    response = serve_blob(request(if_none_match='"nope"', if_modified_since='Sun, 08 Nov 2026 11:00:00 GMT'), BLOB)
    assert response.status_code == 200  # If-None-Match wins over If-Modified-Since


@pytest.mark.parametrize(
    ('since', 'status'),
    [
        ('Sun, 08 Nov 2026 10:00:00 GMT', 304),
        ('Sun, 08 Nov 2026 11:00:00 GMT', 304),
        ('Sun, 08 Nov 2026 09:59:59 GMT', 200),
        ('garbage', 200),
    ],
)
def test_if_modified_since(since: str, status: int) -> None:
    assert serve_blob(request(if_modified_since=since), BLOB).status_code == status
