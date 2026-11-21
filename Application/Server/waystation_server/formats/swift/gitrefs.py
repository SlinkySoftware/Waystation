"""Git tags from a smart-HTTP ref advertisement (``GET <repo>.git/info/refs?service=git-upload-pack``), the one
endpoint GitHub, GitLab, Gitea/Forgejo and plain ``git http-backend`` servers all answer the same way.

The body is pkt-lines (4 hex digits of length, then the line; ``0000`` flushes): ``# service=git-upload-pack``, a
flush, then ``<40 or 64 hex> <ref>`` lines (the first carries ``\\0`` and the capabilities), annotated tags followed by
``<ref>^{}`` with the commit they point at. Only ``refs/tags/*`` matter here; ``tags`` maps each tag name to the
commit it resolves to (peeled when annotated). Malformed input raises ``ValueError``.
"""

from __future__ import annotations

import json
import re

MAX_TAGS = 20_000
_OID = re.compile(rb'^[0-9a-f]{40}([0-9a-f]{24})?$')
_TAG_NAME = re.compile(r'^[0-9A-Za-z._+-]{1,128}$')


def parse_advertisement(data: bytes) -> dict[str, str]:
    """Tag name → commit id (hex) of a ref advertisement."""
    pos = 0
    tags: dict[str, str] = {}
    peeled: dict[str, str] = {}
    seen_lines = 0
    while pos < len(data):
        head = data[pos : pos + 4]
        if len(head) < 4:
            raise ValueError('truncated pkt-line')
        try:
            length = int(head, 16)
        except ValueError:
            raise ValueError('invalid pkt-line length') from None
        if length == 0:
            pos += 4
            continue
        if length < 4 or pos + length > len(data):
            raise ValueError('invalid pkt-line length')
        line = data[pos + 4 : pos + length].rstrip(b'\n')
        pos += length
        seen_lines += 1
        if line.startswith(b'ERR '):
            raise ValueError('the server refused the request')
        if line.startswith(b'#'):
            continue
        line = line.split(b'\x00', 1)[0]
        oid, sep, ref = line.partition(b' ')
        if not sep or not _OID.match(oid):
            if oid == b'0' * 40 and ref.startswith(b'capabilities^{}'):
                continue  # an empty repository
            raise ValueError('malformed ref line')
        if not ref.startswith(b'refs/tags/'):
            continue
        name = ref[len(b'refs/tags/') :].decode('utf-8', errors='replace')
        target = oid.decode('ascii')
        if name.endswith('^{}'):
            peeled[name[:-3]] = target
        elif _TAG_NAME.match(name):
            if len(tags) >= MAX_TAGS:
                continue
            tags[name] = target
    if seen_lines == 0:
        raise ValueError('not a ref advertisement')
    return {name: peeled.get(name, oid) for name, oid in tags.items()}


def compact(tags: dict[str, str]) -> bytes:
    """What the proxy stores instead of the upstream advertisement (sorted, small, JSON)."""
    return json.dumps({'tags': dict(sorted(tags.items()))}, separators=(',', ':')).encode()


def load_compact(data: bytes) -> dict[str, str]:
    doc = json.loads(data)
    tags = doc.get('tags') if isinstance(doc, dict) else None
    if not isinstance(tags, dict):
        raise ValueError('not a stored tag list')
    return {str(k): str(v) for k, v in tags.items()}
