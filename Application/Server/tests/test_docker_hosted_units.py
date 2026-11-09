"""Docker hosted helpers (T-P2-DOCKERH), no I/O: upload paths, manifest checks, tag locks, Content-Range, names."""

import json
from typing import Any

import pytest

from waystation_server.formats.docker import hosted as h
from waystation_server.formats.docker import protocol as p
from waystation_server.formats.docker.descriptor import DESCRIPTOR, DockerOptions
from waystation_server.repos.model import RepoType, StoredHosted, WritePolicy

DIGEST = 'sha256:' + 'a' * 64
OTHER = 'sha256:' + 'b' * 64
SESSION = '0123456789abcdef0123456789abcdef'


@pytest.mark.parametrize(
    ('path', 'expected'),
    [
        ('app/blobs/uploads', p.Operation('upload', 'app', '')),
        (f'team/app/blobs/uploads/{SESSION}', p.Operation('upload', 'team/app', SESSION)),
        (f'app/blobs/{DIGEST}', p.Operation('blob', 'app', DIGEST)),
    ],
)
def test_parse_uploads(path: str, expected: p.Operation) -> None:
    assert p.parse(path) == expected


def test_parse_rejects_bad_session() -> None:
    with pytest.raises(p.RequestRejected) as exc:
        p.parse('app/blobs/uploads/NOT-A-SESSION')
    assert exc.value.code == 'BLOB_UPLOAD_UNKNOWN'
    assert exc.value.status == 404


def _manifest(**fields: Any) -> bytes:
    doc = {
        'schemaVersion': 2,
        'mediaType': p.OCI_MANIFEST,
        'config': {'mediaType': 'application/vnd.oci.image.config.v1+json', 'digest': DIGEST, 'size': 10},
        'layers': [
            {'mediaType': 'application/vnd.oci.image.layer.v1.tar+gzip', 'digest': OTHER, 'size': 20},
            {'mediaType': 'application/vnd.oci.image.layer.nondistributable.v1.tar+gzip', 'digest': OTHER, 'size': 9},
        ],
    }
    doc.update(fields)
    return json.dumps({k: v for k, v in doc.items() if v is not None}).encode()


def test_check_image_manifest() -> None:
    checked = h.check_pushed_manifest(_manifest(), p.OCI_MANIFEST)
    assert checked.media_type == p.OCI_MANIFEST
    assert checked.blobs == ((DIGEST, 10), (OTHER, 20))  # the non-distributable layer is not required
    assert checked.manifests == ()
    assert checked.subject is None and checked.artifact_type is None
    # a generic Content-Type falls back to the document's mediaType
    assert h.check_pushed_manifest(_manifest(), 'application/json').media_type == p.OCI_MANIFEST


def test_check_referrer_and_index() -> None:
    referrer = _manifest(
        config={'mediaType': 'application/vnd.example.sig', 'digest': h.EMPTY_DIGEST, 'size': 2},
        subject={'mediaType': p.OCI_MANIFEST, 'digest': OTHER, 'size': 5},
        annotations={'a': 'b', 'n': 1},
    )
    checked = h.check_pushed_manifest(referrer, p.OCI_MANIFEST)
    assert checked.subject == OTHER
    assert checked.artifact_type == 'application/vnd.example.sig'  # config media type fallback
    assert checked.annotations == {'a': 'b'}
    assert checked.needs_empty
    index = json.dumps(
        {'schemaVersion': 2, 'mediaType': p.OCI_INDEX, 'manifests': [{'mediaType': p.OCI_MANIFEST, 'digest': DIGEST, 'size': 3}]}
    ).encode()
    assert h.check_pushed_manifest(index, p.OCI_INDEX).manifests == (DIGEST,)


@pytest.mark.parametrize(
    ('data', 'content_type', 'message'),
    [
        (b'not json', p.OCI_MANIFEST, 'not valid JSON'),
        (b'[]', p.OCI_MANIFEST, 'not a JSON object'),
        (json.dumps({'schemaVersion': 1}).encode(), p.OCI_MANIFEST, 'schemaVersion 2'),
        (_manifest(mediaType=None), 'text/html', 'unsupported manifest media type'),
        (_manifest(), p.DOCKER_MANIFEST, 'does not match'),
        (_manifest(config={'digest': 'nope', 'size': 1}), p.OCI_MANIFEST, 'invalid digest'),
        (_manifest(config={'digest': DIGEST, 'size': -1}), p.OCI_MANIFEST, 'invalid size'),
        (_manifest(config={'digest': DIGEST, 'size': 1, 'mediaType': 3}), p.OCI_MANIFEST, 'invalid mediaType'),
        (_manifest(layers={}), p.OCI_MANIFEST, 'layers must be a list'),
        (_manifest(config='x'), p.OCI_MANIFEST, 'not a descriptor'),
        (_manifest(artifactType=5), p.OCI_MANIFEST, 'artifactType'),
        (_manifest(mediaType=7), p.OCI_MANIFEST, 'mediaType must be a string'),
        (json.dumps({'schemaVersion': 2, 'mediaType': p.OCI_INDEX}).encode(), p.OCI_INDEX, 'manifests list'),
    ],
)
def test_check_rejects(data: bytes, content_type: str, message: str) -> None:
    with pytest.raises(h.Rejected) as exc:
        h.check_pushed_manifest(data, content_type)
    assert exc.value.status == 400
    assert message in exc.value.message


def test_tag_locks() -> None:
    assert not h.tag_locked(StoredHosted(), 'latest')
    patterns = StoredHosted(immutable_tag_patterns=(r'v\d+(\.\d+)*',))
    assert h.tag_locked(patterns, 'v1.2')
    assert not h.tag_locked(patterns, 'v1.2-rc')  # full match only
    assert h.tag_locked(StoredHosted(write_policy=WritePolicy.DISABLE_REDEPLOY), 'latest')


def test_names_and_ranges() -> None:
    assert h.client_prefix('/v2/hosted/app/blobs/uploads/', '/blobs/uploads') == '/v2/hosted/app'
    assert h.client_prefix('/repository/hosted/v2/app/tags/list', '/tags/list') == '/repository/hosted/v2/app'
    assert h.client_name('/v2/hosted/app') == 'hosted/app'
    assert h.client_name('/repository/hosted/v2/app') == 'app'
    assert h._parse_range(None) is None
    assert h._parse_range('0-99') == (0, 99)
    assert h._parse_range('bytes 100-199/*') == (100, 199)
    for bad in ('5-1', 'x', '-3'):
        with pytest.raises(h.Rejected):
            h._parse_range(bad)


def test_descriptor_options_per_type() -> None:
    options = DockerOptions(aliases=['docker.io'], docker_hub=True)
    assert DESCRIPTOR.check_options(options, RepoType.PROXY) == []
    assert [f for f, _ in DESCRIPTOR.check_options(options, RepoType.HOSTED)] == ['aliases', 'docker_hub']
    assert RepoType.HOSTED in DESCRIPTOR.types
