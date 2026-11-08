"""Browse helpers (T-S09 M0 slice): cursors, folder paths, npm packument and semver parsing, docker manifest and
config parsing."""

import json
from datetime import UTC, datetime

import pytest

from waystation_common.errors import PathRejected, ValidationFailed
from waystation_server.browse.models import decode_cursor, encode_cursor
from waystation_server.browse.tree import folder_prefix
from waystation_server.formats.apt.browse import browse_suite
from waystation_server.formats.docker.browse import parse_config, parse_manifest, platform_key
from waystation_server.formats.npm.browse import parse_packument, version_key

D1 = 'sha256:' + '1' * 64
D2 = 'sha256:' + '2' * 64
D3 = 'sha256:' + '3' * 64


def test_cursor_round_trip_and_rejections() -> None:
    cursor = encode_cursor('f', 'naïve/name')
    assert decode_cursor(cursor, str, str) == ['f', 'naïve/name']
    assert decode_cursor(encode_cursor(40), int) == [40]
    for bad in ('***', encode_cursor('x'), encode_cursor(True), 'e' * 600, encode_cursor({'a': 1})):
        with pytest.raises(ValidationFailed):
            decode_cursor(bad, int)


def test_folder_prefix() -> None:
    assert folder_prefix('') == ''
    assert folder_prefix('/') == ''
    assert folder_prefix('a/b/') == 'a/b/'
    assert folder_prefix('/a//b') == 'a/b/'
    with pytest.raises(PathRejected):
        folder_prefix('a/../../etc')


def test_browse_suite() -> None:
    assert browse_suite('trixie') == 'trixie'
    assert browse_suite('') == '.'


def test_semver_precedence() -> None:
    versions = ['1.0.0', '1.0.0-alpha', '1.0.0-alpha.1', '1.0.0-beta.2', '1.0.0-beta.11', '0.9.9', '10.0.0', 'junk']
    assert sorted(versions, key=version_key, reverse=True) == [
        '10.0.0',
        '1.0.0',
        '1.0.0-beta.11',
        '1.0.0-beta.2',
        '1.0.0-alpha.1',
        '1.0.0-alpha',
        '0.9.9',
        'junk',
    ]


def packument(**extra: object) -> bytes:
    doc = {
        'name': 'is-odd',
        'dist-tags': {'latest': '3.0.1', 'next': '4.0.0-rc.1'},
        'versions': {
            '3.0.1': {
                'dist': {'tarball': 'https://registry.example.com/is-odd/-/is-odd-3.0.1.tgz', 'integrity': 'sha512-x'},
                'license': 'MIT',
            },
            '4.0.0-rc.1': {
                'dist': {'tarball': 'https://registry.example.com/is-odd/-/is-odd-4.0.0-rc.1.tgz', 'unpackedSize': 9},
                'deprecated': 'use 3.x',
            },
            '1.0.0': {'dist': {}},
        },
        **extra,
    }
    return json.dumps(doc).encode()


def test_parse_full_packument() -> None:
    parsed = parse_packument(
        packument(
            description='Is it odd?',
            time={'3.0.1': '2026-01-02T03:04:05.000Z', 'modified': '2026-02-01T00:00:00Z'},
        ),
        'is-odd',
    )
    assert [r.version for r in parsed.rows] == ['4.0.0-rc.1', '3.0.1', '1.0.0']
    assert parsed.latest == '3.0.1'
    assert parsed.license == 'MIT'  # from the latest version when the packument has none
    assert parsed.description == 'Is it odd?'
    assert parsed.modified == datetime(2026, 2, 1, tzinfo=UTC)
    rc, latest, old = parsed.rows
    assert latest.published == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert latest.filename == 'is-odd-3.0.1.tgz'
    assert latest.integrity == 'sha512-x'
    assert rc.deprecated == 'use 3.x'
    assert rc.unpacked_size == 9
    assert old.filename is None
    assert parsed.summary() == {
        'latest': '3.0.1',
        'dist_tags': {'latest': '3.0.1', 'next': '4.0.0-rc.1'},
        'version_count': 3,
        'modified': parsed.modified,
        'description': 'Is it odd?',
        'license': 'MIT',
    }


def test_parse_abbreviated_packument_keeps_stored_description() -> None:
    parsed = parse_packument(json.dumps({'name': 'x', 'versions': {'1.0.0': {}}}).encode(), 'x')
    assert parsed.latest == '1.0.0'  # no dist-tags: the newest version
    assert 'description' not in parsed.summary()
    assert 'license' not in parsed.summary()


@pytest.mark.parametrize('data', [b'nope', b'[]'])
def test_parse_packument_rejects(data: bytes) -> None:
    with pytest.raises(ValidationFailed):
        parse_packument(data, 'x')


def test_parse_image_manifest() -> None:
    manifest = {
        'schemaVersion': 2,
        'mediaType': 'application/vnd.oci.image.manifest.v1+json',
        'config': {'digest': D1, 'size': 100, 'mediaType': 'application/vnd.oci.image.config.v1+json'},
        'layers': [
            {'digest': D2, 'size': 1000, 'mediaType': 'application/vnd.oci.image.layer.v1.tar+gzip'},
            {'digest': 'md5:nope', 'size': 5},
            {'digest': D3, 'size': -1},
        ],
        'subject': {'digest': D3},
    }
    record = parse_manifest(json.dumps(manifest).encode(), None)
    assert record['is_index'] is False
    assert record['config_digest'] == D1
    assert [layer['digest'] for layer in record['layers']] == [D2, D3]
    assert record['size_total'] == 1100
    assert record['subject_digest'] == D3


def test_parse_index_drops_attestations_from_platforms() -> None:
    index = {
        'mediaType': 'application/vnd.oci.image.index.v1+json',
        'manifests': [
            {'digest': D1, 'size': 500, 'platform': {'os': 'linux', 'architecture': 'amd64'}},
            {'digest': D2, 'size': 400, 'platform': {'os': 'linux', 'architecture': 'arm64', 'variant': 'v8'}},
            {'digest': D3, 'size': 300, 'platform': {'os': 'unknown', 'architecture': 'unknown'}},
        ],
    }
    record = parse_manifest(json.dumps(index).encode(), 'application/json')
    assert record['is_index'] is True
    assert len(record['manifests']) == 3
    assert record['platforms'] == [
        {'os': 'linux', 'architecture': 'amd64', 'variant': None},
        {'os': 'linux', 'architecture': 'arm64', 'variant': 'v8'},
    ]
    with pytest.raises(ValidationFailed):
        parse_manifest(b'[1]', None)


def test_parse_config() -> None:
    config = {
        'os': 'linux',
        'architecture': 'arm64',
        'variant': 'v8',
        'created': '2026-03-04T05:06:07Z',
        'config': {'Labels': {'org.opencontainers.image.version': '1.2', 'bad': 3}},
        'history': [
            {'created_by': 'ADD rootfs.tar /'},
            {'created_by': 'ENV A=b', 'empty_layer': True},
            {'created_by': 'RUN make'},
        ],
    }
    parsed = parse_config(json.dumps(config).encode())
    assert parsed.platform is not None
    assert platform_key(parsed.platform) == 'linux/arm64/v8'
    assert parsed.created == datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    assert parsed.labels == {'org.opencontainers.image.version': '1.2'}
    assert parsed.layer_history == ['ADD rootfs.tar /', 'RUN make']
    assert parse_config(b'not json').platform is None
