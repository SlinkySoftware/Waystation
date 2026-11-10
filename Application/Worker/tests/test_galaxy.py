"""Ansible collection imports (``waystation_worker.galaxy``, job ``import.galaxy``): artifact validation without
extraction (MANIFEST.json, FILES.json, per-file sha256, tar-slip guards)."""

import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest

from waystation_common import galaxy
from waystation_common.jobs import JOB_TYPES, GalaxyImport
from waystation_worker.galaxy import ImportFailed, ImportResult, clean_name, read_collection
from waystation_worker.jobs.registry import HANDLERS

EXPECTED = galaxy.Artifact('harness', 'demo', '1.0.0')
RUNTIME = b"---\nrequires_ansible: '>=2.15.0'  # the oldest supported\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(
    tmp_path: Path,
    files: dict[str, bytes] | None = None,
    *,
    info: dict[str, Any] | None = None,
    listed: list[dict[str, Any]] | None = None,
    files_json: bytes | None = None,
    manifest: dict[str, Any] | None = None,
    extra: list[tarfile.TarInfo] | None = None,
    skip: tuple[str, ...] = (),
) -> Path:
    """A collection artifact as ``ansible-galaxy collection build`` writes it (overridable piece by piece)."""
    files = (
        {'README.md': b'# demo\n', 'meta/runtime.yml': RUNTIME, 'plugins/README.md': b'x'} if files is None else files
    )
    entries = [{'name': '.', 'ftype': 'dir', 'chksum_type': None, 'chksum_sha256': None, 'format': 1}]
    for name, data in files.items():
        entries.append(
            {'name': name, 'ftype': 'file', 'chksum_type': 'sha256', 'chksum_sha256': sha(data), 'format': 1}
        )
    raw_files = files_json if files_json is not None else json.dumps({'files': listed or entries, 'format': 1}).encode()
    collection_info = {
        'namespace': 'harness',
        'name': 'demo',
        'version': '1.0.0',
        'authors': ['someone', 5],
        'description': 'A demo',
        'license': ['GPL-3.0-or-later'],
        'tags': ['demo'],
        'dependencies': {'community.general': '>=9.0.0'},
        'repository': 'https://git.example.com/demo',
        **(info or {}),
    }
    doc = manifest or {
        'collection_info': collection_info,
        'file_manifest_file': {
            'name': 'FILES.json',
            'ftype': 'file',
            'chksum_type': 'sha256',
            'chksum_sha256': sha(raw_files),
            'format': 1,
        },
        'format': 1,
    }
    members = {'MANIFEST.json': json.dumps(doc).encode(), 'FILES.json': raw_files, **files}
    path = tmp_path / EXPECTED.filename
    with tarfile.open(path, mode='w:gz') as tar:
        for name, data in members.items():
            if name in skip:
                continue
            item = tarfile.TarInfo(name)
            item.size = len(data)
            tar.addfile(item, io.BytesIO(data))
        for item in extra or []:
            tar.addfile(item)
    return path


def link(name: str, target: str) -> tarfile.TarInfo:
    item = tarfile.TarInfo(name)
    item.type = tarfile.SYMTYPE
    item.linkname = target
    return item


def test_a_built_collection_is_accepted(tmp_path: Path) -> None:
    folder = tarfile.TarInfo('plugins')
    folder.type = tarfile.DIRTYPE
    found = read_collection(build(tmp_path, extra=[folder]), EXPECTED)
    assert (found.namespace, found.name, found.version, found.files) == ('harness', 'demo', '1.0.0', 3)
    assert found.requires_ansible == '>=2.15.0'
    assert found.metadata['authors'] == ['someone'] and found.metadata['dependencies'] == {
        'community.general': '>=9.0.0'
    }
    assert found.metadata['description'] == 'A demo' and found.metadata['repository'] == 'https://git.example.com/demo'


def test_symlinks_inside_the_collection_are_followed(tmp_path: Path) -> None:
    data = b'shared'
    listed = [
        {'name': 'docs/a.md', 'ftype': 'file', 'chksum_type': 'sha256', 'chksum_sha256': sha(data)},
        {'name': 'docs/b.md', 'ftype': 'file', 'chksum_type': 'sha256', 'chksum_sha256': sha(data)},
    ]
    path = build(tmp_path, {'docs/a.md': data}, listed=listed, extra=[link('docs/b.md', 'a.md')])
    assert read_collection(path, EXPECTED).files == 2


@pytest.mark.parametrize(
    ('kwargs', 'code'),
    [
        ({'skip': ('MANIFEST.json',)}, 'invalid_manifest'),
        ({'skip': ('FILES.json',)}, 'invalid_manifest'),
        ({'info': {'version': '1.0.1'}}, 'mismatch'),
        ({'info': {'name': 'Demo'}}, 'invalid_manifest'),
        ({'info': {'namespace': 5}}, 'invalid_manifest'),
        ({'info': {'version': '1.0'}}, 'invalid_manifest'),
        ({'info': {'dependencies': ['x']}}, 'invalid_manifest'),
        ({'info': {'dependencies': {'x': 1}}}, 'invalid_manifest'),
        ({'info': {'dependencies': {'a.b.c-d': '*'}}}, 'invalid_manifest'),
        ({'manifest': {'collection_info': 'x'}}, 'invalid_manifest'),
        ({'files_json': b'not json'}, 'invalid_manifest'),
        ({'files_json': b'{"files": {}}'}, 'invalid_manifest'),
        ({'listed': [{'ftype': 'file'}]}, 'invalid_manifest'),
        ({'listed': [{'name': 'README.md', 'ftype': 'link'}]}, 'invalid_manifest'),
        ({'listed': [{'name': 'README.md', 'ftype': 'file', 'chksum_sha256': 'x'}]}, 'invalid_manifest'),
        ({'listed': [{'name': 'README.md', 'ftype': 'file', 'chksum_sha256': '0' * 64}]}, 'checksum_mismatch'),
        ({'listed': [{'name': 'gone.md', 'ftype': 'file', 'chksum_sha256': '0' * 64}]}, 'missing_file'),
        ({'files': {'README.md': b'x'}, 'listed': [{'name': '.', 'ftype': 'dir'}]}, 'unlisted_file'),
    ],
)
def test_bad_collections_are_refused(tmp_path: Path, kwargs: dict[str, Any], code: str) -> None:
    with pytest.raises(ImportFailed) as caught:
        read_collection(build(tmp_path, **kwargs), EXPECTED)
    assert caught.value.code == code


def test_manifest_must_name_files_json_with_its_checksum(tmp_path: Path) -> None:
    info = {'namespace': 'harness', 'name': 'demo', 'version': '1.0.0'}
    for file_manifest, code in (
        ({'name': 'OTHER.json'}, 'invalid_manifest'),
        ({'name': 'FILES.json', 'chksum_sha256': 'x'}, 'invalid_manifest'),
        ({'name': 'FILES.json', 'chksum_sha256': '0' * 64}, 'checksum_mismatch'),
    ):
        manifest = {'collection_info': info, 'file_manifest_file': file_manifest}
        with pytest.raises(ImportFailed) as caught:
            read_collection(build(tmp_path, {}, manifest=manifest), EXPECTED)
        assert caught.value.code == code


@pytest.mark.parametrize(
    'member',
    [
        link('evil', '/etc/passwd'),
        link('evil', '../../etc/passwd'),
        link('evil', '.'),
        link('evil', 'a\\b'),
        tarfile.TarInfo('../escape'),
        tarfile.TarInfo('/abs'),
        tarfile.TarInfo('a\\b'),
        tarfile.TarInfo('bad\x01name'),
        tarfile.TarInfo('/'.join(['d'] * 70)),
    ],
)
def test_unsafe_members_are_refused(tmp_path: Path, member: tarfile.TarInfo) -> None:
    with pytest.raises(ImportFailed) as caught:
        read_collection(build(tmp_path, extra=[member]), EXPECTED)
    assert caught.value.code == 'invalid_archive'


def test_special_files_duplicates_and_garbage_are_refused(tmp_path: Path) -> None:
    fifo = tarfile.TarInfo('pipe')
    fifo.type = tarfile.FIFOTYPE
    with pytest.raises(ImportFailed, match='regular file'):
        read_collection(build(tmp_path, extra=[fifo]), EXPECTED)
    with pytest.raises(ImportFailed, match='twice'):
        read_collection(build(tmp_path, extra=[link('README.md', 'plugins/README.md')]), EXPECTED)
    with pytest.raises(ImportFailed, match='nest too deeply'):
        read_collection(
            build(
                tmp_path,
                {},
                listed=[{'name': 'a', 'ftype': 'file', 'chksum_sha256': '0' * 64}],
                extra=[link('a', 'b'), link('b', 'a')],
            ),
            EXPECTED,
        )
    junk = tmp_path / 'junk.tar.gz'
    junk.write_bytes(b'not a tarball')
    with pytest.raises(ImportFailed, match='gzip'):
        read_collection(junk, EXPECTED)
    cut = tmp_path / 'cut.tar.gz'
    cut.write_bytes(build(tmp_path).read_bytes()[:200])
    with pytest.raises(ImportFailed, match='damaged'):
        read_collection(cut, EXPECTED)


def test_clean_name() -> None:
    assert clean_name('./a//b/./c') == 'a/b/c' and clean_name('./') == ''


def test_the_job_is_registered() -> None:
    assert JOB_TYPES['import.galaxy'] is GalaxyImport
    assert GalaxyImport.kind in HANDLERS
    assert GalaxyImport(task_id='t1').dedupe_key() == 'import.galaxy:t1'
    assert ImportResult('t1', 'completed', 'x').as_dict() == {'task_id': 't1', 'state': 'completed', 'detail': 'x'}
