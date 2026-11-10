"""Minimal RPM files for tests: a lead, a signature header (with the main header's SHA-256) and a main header with
the tags hosted yum repositories read. The payload is opaque bytes (never unpacked by Waystation)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

STRING, INT32, STRING_ARRAY, I18NSTRING, INT16 = 6, 4, 8, 9, 3
DIR_MODE, FILE_MODE = 0o40755, 0o100644
Value = tuple[int, object]  # (type, value)


def header(tags: dict[int, Value]) -> bytes:
    index = b''
    store = bytearray()
    for tag, (kind, value) in sorted(tags.items()):
        if kind in (INT32,):
            while len(store) % 4:
                store.append(0)
        if kind == INT16:
            while len(store) % 2:
                store.append(0)
        offset = len(store)
        if kind == STRING:
            store += str(value).encode() + b'\x00'
            count = 1
        elif kind in (STRING_ARRAY, I18NSTRING):
            items = list(value)  # type: ignore[call-overload]
            for item in items:
                store += str(item).encode() + b'\x00'
            count = len(items)
        elif kind == INT32:
            items = list(value)  # type: ignore[call-overload]
            for item in items:
                store += int(item).to_bytes(4, 'big')
            count = len(items)
        else:  # INT16
            items = list(value)  # type: ignore[call-overload]
            for item in items:
                store += int(item).to_bytes(2, 'big')
            count = len(items)
        index += b''.join(x.to_bytes(4, 'big') for x in (tag, kind, offset, count))
    n = len(tags)
    return b'\x8e\xad\xe8\x01\x00\x00\x00\x00' + n.to_bytes(4, 'big') + len(store).to_bytes(4, 'big') + index + store


def build_rpm(
    name: str,
    version: str = '1.0',
    release: str = '1.el10',
    arch: str = 'noarch',
    *,
    epoch: int | None = None,
    summary: str = 'A test package',
    requires: Sequence[str] = ('/bin/sh',),
    files: Sequence[str] = ('/usr/bin/hello', '/etc/hello.conf'),
    dirs: Sequence[str] = ('/usr/share/hello',),
    payload: bytes = b'payload',
    extra: dict[int, Value] | None = None,
    digest: str | None = None,
    source: bool = False,
) -> bytes:
    """An RPM whose header describes the given package (and provides itself)."""
    paths = [*files, *dirs]
    dirnames = sorted({p.rsplit('/', 1)[0] + '/' for p in paths})
    tags: dict[int, Value] = {
        1000: (STRING, name),
        1001: (STRING, version),
        1002: (STRING, release),
        1004: (I18NSTRING, [summary]),
        1005: (I18NSTRING, [f'{summary} & <more>']),
        1006: (INT32, [1_700_000_000]),
        1007: (STRING, 'builder.example.com'),
        1009: (INT32, [1234]),
        1014: (STRING, 'MIT'),
        1016: (I18NSTRING, ['Unspecified']),
        1022: (STRING, arch),
        1044: (STRING, f'{name}-{version}-{release}.src.rpm'),
        1047: (STRING_ARRAY, [name]),
        1112: (INT32, [8]),
        1113: (STRING_ARRAY, [f'{version}-{release}']),
        1048: (INT32, [0, 1 << 24] + [0] * (len(requires) - 1) if requires else [1 << 24]),
        1049: (
            STRING_ARRAY,
            [requires[0], 'rpmlib(CompressedFileNames)', *requires[1:]] if requires else ['rpmlib(X)'],
        ),
        1050: (STRING_ARRAY, ['', '3.0.4-1'] + [''] * (len(requires) - 1) if requires else ['3.0.4-1']),
        1116: (INT32, [dirnames.index(p.rsplit('/', 1)[0] + '/') for p in paths]),
        1117: (STRING_ARRAY, [p.rsplit('/', 1)[1] for p in paths]),
        1118: (STRING_ARRAY, dirnames),
        1030: (INT16, [FILE_MODE] * len(files) + [DIR_MODE] * len(dirs)),
        1037: (INT32, [0] * len(paths)),
        1080: (INT32, [1_700_000_000]),
        1081: (STRING_ARRAY, ['Test Packager <packager@example.com> - 1.0-1']),
        1082: (STRING_ARRAY, ['- First build']),
    }
    if epoch is not None:
        tags[1003] = (INT32, [epoch])
    if source:
        del tags[1044]
    tags.update(extra or {})
    main = header(tags)
    sha = digest if digest is not None else hashlib.sha256(main).hexdigest()
    signature = header({273: (STRING, sha)} if sha else {1000: (INT32, [len(main)])})
    pad = b'\x00' * ((-len(signature)) % 8)
    lead = b'\xed\xab\xee\xdb\x03\x00' + b'\x00\x00' + b'\x00\x01' + name.encode()[:65].ljust(66, b'\x00')
    lead += b'\x00\x01' + b'\x00\x05' + b'\x00' * 16
    assert len(lead) == 96
    return lead + signature + pad + main + payload
