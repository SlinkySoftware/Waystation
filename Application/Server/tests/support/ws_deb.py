"""Build small Debian binary packages in Python for the hosted apt tests (no dpkg-deb in the dev image)."""

from __future__ import annotations

import gzip
import io
import lzma
import tarfile
import time


def ar_member(name: str, data: bytes) -> bytes:
    header = f'{name:<16}{0:<12}{0:<6}{0:<6}{"100644":<8}{len(data):<10}'.encode() + b'`\n'
    return header + data + (b'\n' if len(data) % 2 else b'')


def tar_of(files: dict[str, bytes], *, compression: str = 'gz') -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w', format=tarfile.GNU_FORMAT) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    data = raw.getvalue()
    if compression == 'gz':
        return gzip.compress(data, mtime=0)
    if compression == 'xz':
        return lzma.compress(data, format=lzma.FORMAT_XZ)
    if compression == 'zst':
        from compression import zstd

        return zstd.compress(data)
    return data


def control_text(name: str, version: str, arch: str, **extra: str) -> str:
    fields = {
        'Package': name,
        'Version': version,
        'Architecture': arch,
        'Maintainer': 'Harness <harness@example.com>',
        'Section': 'misc',
        'Priority': 'optional',
        **extra,
        'Description': f'{name} for tests\n A package built by the Waystation tests.\n .\n -dash line kept.',
    }
    return ''.join(f'{k}: {v}\n' for k, v in fields.items())


def build_deb(
    name: str,
    version: str,
    arch: str = 'all',
    *,
    control: str | None = None,
    compression: str = 'gz',
    members: list[tuple[str, bytes]] | None = None,
    **extra: str,
) -> bytes:
    """A ``.deb``: ``debian-binary``, ``control.tar.<compression>``, ``data.tar.<compression>``; ``members``
    replaces the member list (malformed packages)."""
    text = control if control is not None else control_text(name, version, arch, **extra)
    suffix = '' if compression == 'none' else f'.{compression}'
    if members is None:
        members = [
            ('debian-binary', b'2.0\n'),
            (f'control.tar{suffix}', tar_of({'./control': text.encode(), './md5sums': b''}, compression=compression)),
            (
                f'data.tar{suffix}',
                tar_of({f'./usr/share/{name}/hello.txt': f'hello {version}\n'.encode()}, compression=compression),
            ),
        ]
    return b'!<arch>\n' + b''.join(ar_member(n, d) for n, d in members)
