"""Write a tiny wheel and sdist of ws-harness-demo <version> into <dir> (no build backend, no network).

python3 make_dist.py <dir> <version>
"""

import base64
import hashlib
import io
import sys
import tarfile
import zipfile
from pathlib import Path

NAME, DIST = 'ws-harness-demo', 'ws_harness_demo'


def metadata(version: str) -> bytes:
    return (
        f'Metadata-Version: 2.1\nName: {NAME}\nVersion: {version}\nSummary: Waystation client harness demo\n'
        'Requires-Python: >=3.9\nLicense: GPL-3.0-or-later\n'
    ).encode()


def record_line(path: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
    return f'{path},sha256={digest},{len(data)}'


def wheel(out: Path, version: str) -> None:
    info = f'{DIST}-{version}.dist-info'
    files = {
        f'{DIST}/__init__.py': f'VERSION = {version!r}\n'.encode(),
        f'{info}/METADATA': metadata(version),
        f'{info}/WHEEL': b'Wheel-Version: 1.0\nGenerator: make_dist\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
    }
    record = '\n'.join([*(record_line(p, d) for p, d in files.items()), f'{info}/RECORD,,']) + '\n'
    with zipfile.ZipFile(out / f'{DIST}-{version}-py3-none-any.whl', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items():
            archive.writestr(path, data)
        archive.writestr(f'{info}/RECORD', record)


def sdist(out: Path, version: str) -> None:
    files = {'PKG-INFO': metadata(version), f'{DIST}/__init__.py': f'VERSION = {version!r}\n'.encode()}
    with tarfile.open(out / f'{DIST}-{version}.tar.gz', 'w:gz') as archive:
        for path, data in files.items():
            member = tarfile.TarInfo(f'{DIST}-{version}/{path}')
            member.size, member.mtime = len(data), 1_700_000_000
            archive.addfile(member, io.BytesIO(data))


if __name__ == '__main__':
    target, ver = Path(sys.argv[1]), sys.argv[2]
    target.mkdir(parents=True, exist_ok=True)
    wheel(target, ver)
    sdist(target, ver)
