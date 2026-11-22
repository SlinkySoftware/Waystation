"""Tiny conda packages and repodata for tests (FORMATS.md "Conda")."""

from __future__ import annotations

import bz2
import hashlib
import io
import json
import tarfile
import zipfile
from typing import Any


def index_json(name: str, version: str, build: str, subdir: str = 'noarch', **extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        'name': name,
        'version': version,
        'build': build,
        'build_number': 0,
        'subdir': subdir,
        'depends': ['python >=3.9'],
        'license': 'MIT',
        'timestamp': 1_700_000_000_000,
    }
    if subdir == 'noarch':
        doc['noarch'] = 'python'
    doc.update(extra)
    return doc


def _tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def info_files(index: dict[str, Any] | bytes) -> dict[str, bytes]:
    raw = index if isinstance(index, bytes) else json.dumps(index).encode()
    return {'info/about.json': b'{}', 'info/index.json': raw, 'info/paths.json': b'{"paths": []}'}


def conda_package(index: dict[str, Any] | bytes, *, metadata: bytes | None = None, members: int = 0) -> bytes:
    """A ``.conda`` (v2) package: zip of metadata.json, info-*.tar.zst and pkg-*.tar.zst."""
    from compression import zstd

    stem = 'pkg'
    if isinstance(index, dict):
        stem = f'{index["name"]}-{index["version"]}-{index["build"]}'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('metadata.json', metadata if metadata is not None else b'{"conda_pkg_format_version": 2}')
        archive.writestr(f'pkg-{stem}.tar.zst', zstd.compress(_tar({'site-packages/demo.py': b'X = 1\n'})))
        archive.writestr(f'info-{stem}.tar.zst', zstd.compress(_tar(info_files(index))))
        for n in range(members):
            archive.writestr(f'extra-{n}', b'')
    return buffer.getvalue()


def tar_bz2_package(index: dict[str, Any] | bytes) -> bytes:
    """A ``.tar.bz2`` (v1) package: the payload first, info/ last (as old conda-build wrote them)."""
    return bz2.compress(_tar({'site-packages/demo.py': b'X = 1\n', **info_files(index)}))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def entry(index: dict[str, Any], data: bytes) -> dict[str, Any]:
    return {
        **index,
        'md5': hashlib.md5(data, usedforsecurity=False).hexdigest(),
        'sha256': sha256(data),
        'size': len(data),
    }


def repodata(subdir: str, files: dict[str, dict[str, Any]]) -> bytes:
    doc = {
        'info': {'subdir': subdir},
        'packages': {k: v for k, v in files.items() if k.endswith('.tar.bz2')},
        'packages.conda': {k: v for k, v in files.items() if k.endswith('.conda')},
        'removed': [],
        'repodata_version': 1,
    }
    return json.dumps(doc, indent=1).encode()
