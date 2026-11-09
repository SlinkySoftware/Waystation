"""Hosted Ansible collection imports (job ``import.galaxy``; FORMATS.md "Ansible Galaxy"; ARCHITECTURE §10).

``ansible-galaxy collection publish`` uploads an artifact; the server streams it into the blob store (verifying the
``sha256`` form field), records an import task (``galaxy_imports``) and, in the same transaction, puts this job in
the outbox. The client polls the task until it is ``completed`` or ``failed``. This job:

1. reads the artifact without extracting it (``read_collection``, in a thread): a gzip tarball of regular files,
   directories and symbolic links only (no absolute names, ``..``, backslashes or control characters, link targets
   inside the collection), bounded in members, sizes and nesting; ``MANIFEST.json`` (``collection_info`` naming the
   uploaded namespace, name and SemVer version) and ``FILES.json`` (its sha256 must equal the manifest's
   ``file_manifest_file`` checksum); every file FILES.json lists must be in the archive with that sha256, and every
   file in the archive must be listed;
2. publishes the version: the ``galaxy_collections`` record (unique: a version that exists already fails the
   import, versions are immutable), then the artifact asset ``collections/<ns>/<name>/<file>``;
3. finishes the task with its messages (or the error).

The job is idempotent: a finished task is left alone, and a retry after a crash between steps finds its own record.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import posixpath
import re
import tarfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from waystation_common import galaxy
from waystation_common.db import galaxy as galaxy_db
from waystation_common.db.client import Database, Document
from waystation_common.storage import BlobStore

MAX_MEMBERS = 20_000
MAX_UNPACKED = 2 * 1024 * 1024 * 1024
MAX_MANIFEST = 4 * 1024 * 1024
MAX_FILES_JSON = 32 * 1024 * 1024
MAX_RUNTIME = 1024 * 1024
MAX_NAME_BYTES = 1024
MAX_DEPTH = 64
MAX_LINK_HOPS = 8
MAX_TEXT = 4096
MAX_LIST = 100
CHUNK = 1024 * 1024
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_HEX64 = re.compile(r'^[0-9a-f]{64}$')
_REQUIRES = re.compile(r'''^requires_ansible\s*:\s*['"]?([^'"#\r\n]{1,200}?)['"]?\s*(?:#.*)?$''', re.MULTILINE)
INFO_TEXT = ('description', 'repository', 'documentation', 'homepage', 'issues', 'license_file', 'readme')
INFO_LISTS = ('authors', 'license', 'tags')


class ImportFailed(Exception):  # noqa: N818 - an outcome the client sees
    def __init__(self, code: str, description: str) -> None:
        super().__init__(description)
        self.code = code
        self.description = description


@dataclass(frozen=True)
class Collection:
    namespace: str
    name: str
    version: str
    metadata: dict[str, Any]
    requires_ansible: str | None
    files: int
    unpacked: int
    messages: list[str] = field(default_factory=list)


def clean_name(name: str) -> str:
    """A member name as a relative POSIX path (``./`` prefixes dropped), or ``ImportFailed``."""
    if len(name.encode('utf-8', 'surrogateescape')) > MAX_NAME_BYTES or _CONTROL.search(name) or '\\' in name:
        raise ImportFailed('invalid_archive', f'unsafe file name in the archive: {name[:200]!r}')
    if name.startswith('/'):
        raise ImportFailed('invalid_archive', f'absolute path in the archive: {name[:200]!r}')
    parts = [p for p in name.split('/') if p not in ('', '.')]
    if any(p == '..' for p in parts):
        raise ImportFailed('invalid_archive', f'".." in an archive path: {name[:200]!r}')
    if len(parts) > MAX_DEPTH:
        raise ImportFailed('invalid_archive', 'the archive nests too deeply')
    return '/'.join(parts)


def _link_target(name: str, target: str) -> str:
    if not target or target.startswith('/') or _CONTROL.search(target) or '\\' in target:
        raise ImportFailed('invalid_archive', f'unsafe link target for {name!r}')
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    if resolved in ('.', '') or resolved == '..' or resolved.startswith('../'):
        raise ImportFailed('invalid_archive', f'link {name!r} points outside the collection')
    return resolved


def _read(handle: IO[bytes], limit: int, what: str) -> bytes:
    data = handle.read(limit + 1)
    if len(data) > limit:
        raise ImportFailed('invalid_archive', f'{what} is too large')
    return data


def _hash(handle: IO[bytes]) -> str:
    digest = hashlib.sha256()
    while chunk := handle.read(CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def _json(data: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise ImportFailed('invalid_manifest', f'{what} is not valid JSON') from None
    if not isinstance(value, dict):
        raise ImportFailed('invalid_manifest', f'{what} is not a JSON object')
    return value


def _metadata(info: dict[str, Any]) -> dict[str, Any]:
    """The ``collection_info`` fields served in version details, type-checked and bounded."""
    out: dict[str, Any] = {}
    for key in INFO_TEXT:
        value = info.get(key)
        if isinstance(value, str) and value:
            out[key] = value[:MAX_TEXT]
    for key in INFO_LISTS:
        value = info.get(key)
        if isinstance(value, list):
            out[key] = [str(v)[:MAX_TEXT] for v in value[:MAX_LIST] if isinstance(v, str)]
    deps = info.get('dependencies') or {}
    if not isinstance(deps, dict) or len(deps) > MAX_LIST:
        raise ImportFailed('invalid_manifest', 'MANIFEST.json dependencies must be a mapping')
    dependencies: dict[str, str] = {}
    for key, value in deps.items():
        if not isinstance(key, str) or not isinstance(value, str) or len(key) > 200 or len(value) > 200:
            raise ImportFailed('invalid_manifest', 'MANIFEST.json dependencies must map names to version ranges')
        ns, _, nm = key.partition('.')
        if not galaxy.valid_name(ns, strict=False) or not galaxy.valid_name(nm, strict=False):
            raise ImportFailed('invalid_manifest', f'invalid dependency name {key[:200]!r}')
        dependencies[key] = value
    out['dependencies'] = dependencies
    return out


def read_collection(path: Path, expected: galaxy.Artifact) -> Collection:  # noqa: C901 - one linear validation
    """Validate a collection artifact (blocking: run in a thread). Raises ``ImportFailed``."""
    try:
        tar = tarfile.open(path, mode='r:gz')  # noqa: SIM115 - closed below
    except (tarfile.TarError, OSError, EOFError):
        raise ImportFailed('invalid_archive', 'the artifact is not a gzip-compressed tar archive') from None
    regular: dict[str, str] = {}  # name -> sha256
    links: dict[str, str] = {}  # name -> resolved target
    dirs: set[str] = set()
    manifest_raw = files_raw = runtime_raw = None
    unpacked = members = 0
    try:
        with tar:
            for member in tar:
                members += 1
                if members > MAX_MEMBERS:
                    raise ImportFailed('invalid_archive', f'the archive has more than {MAX_MEMBERS} members')
                name = clean_name(member.name)
                if not name:
                    if member.isdir():
                        continue
                    raise ImportFailed('invalid_archive', 'an archive member has an empty name')
                if name in regular or name in links:
                    raise ImportFailed('invalid_archive', f'{name!r} appears twice in the archive')
                if member.isdir():
                    dirs.add(name)
                    continue
                if member.issym():
                    links[name] = _link_target(name, member.linkname)
                    continue
                if not member.isfile():
                    raise ImportFailed('invalid_archive', f'{name!r} is not a regular file, directory or symlink')
                unpacked += member.size
                if unpacked > MAX_UNPACKED:
                    raise ImportFailed('invalid_archive', 'the archive unpacks to more than 2 GiB')
                handle = tar.extractfile(member)
                if handle is None:
                    raise ImportFailed('invalid_archive', f'cannot read {name!r}')
                with handle:
                    if name == 'MANIFEST.json':
                        manifest_raw = _read(handle, MAX_MANIFEST, 'MANIFEST.json')
                        regular[name] = hashlib.sha256(manifest_raw).hexdigest()
                    elif name == 'FILES.json':
                        files_raw = _read(handle, MAX_FILES_JSON, 'FILES.json')
                        regular[name] = hashlib.sha256(files_raw).hexdigest()
                    elif name == 'meta/runtime.yml' and member.size <= MAX_RUNTIME:
                        runtime_raw = handle.read(MAX_RUNTIME)
                        regular[name] = hashlib.sha256(runtime_raw).hexdigest()
                    else:
                        regular[name] = _hash(handle)
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        raise ImportFailed('invalid_archive', f'the archive is damaged ({type(exc).__name__})') from None
    if manifest_raw is None:
        raise ImportFailed('invalid_manifest', 'the archive has no MANIFEST.json at its root')
    if files_raw is None:
        raise ImportFailed('invalid_manifest', 'the archive has no FILES.json at its root')
    manifest = _json(manifest_raw, 'MANIFEST.json')
    info = manifest.get('collection_info')
    if not isinstance(info, dict):
        raise ImportFailed('invalid_manifest', 'MANIFEST.json has no collection_info')
    namespace, name, version = info.get('namespace'), info.get('name'), info.get('version')
    if not isinstance(namespace, str) or not galaxy.valid_name(namespace):
        raise ImportFailed('invalid_manifest', f'invalid namespace {str(namespace)[:100]!r} in MANIFEST.json')
    if not isinstance(name, str) or not galaxy.valid_name(name):
        raise ImportFailed('invalid_manifest', f'invalid collection name {str(name)[:100]!r} in MANIFEST.json')
    if not isinstance(version, str) or not galaxy.valid_version(version):
        raise ImportFailed('invalid_manifest', f'invalid version {str(version)[:100]!r} in MANIFEST.json (SemVer)')
    if (namespace, name, version) != (expected.namespace, expected.name, expected.version):
        raise ImportFailed(
            'mismatch',
            f'MANIFEST.json describes {namespace}.{name} {version}, the file name {expected.filename}',
        )
    file_manifest = manifest.get('file_manifest_file')
    if not isinstance(file_manifest, dict) or file_manifest.get('name') != 'FILES.json':
        raise ImportFailed('invalid_manifest', 'MANIFEST.json does not name FILES.json as its file manifest')
    if file_manifest.get('chksum_type', 'sha256') != 'sha256' or not _HEX64.match(
        str(file_manifest.get('chksum_sha256', ''))
    ):
        raise ImportFailed('invalid_manifest', 'MANIFEST.json has no sha256 checksum for FILES.json')
    if regular['FILES.json'] != file_manifest['chksum_sha256']:
        raise ImportFailed('checksum_mismatch', 'FILES.json does not match its checksum in MANIFEST.json')
    files_doc = _json(files_raw, 'FILES.json')
    entries = files_doc.get('files')
    if not isinstance(entries, list) or len(entries) > MAX_MEMBERS:
        raise ImportFailed('invalid_manifest', 'FILES.json has no list of files')
    listed: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('name'), str):
            raise ImportFailed('invalid_manifest', 'FILES.json has an entry without a name')
        entry_name = clean_name(entry['name'])
        ftype = entry.get('ftype')
        if ftype == 'dir' or not entry_name:
            continue
        if ftype != 'file':
            raise ImportFailed('invalid_manifest', f'FILES.json: unknown type {str(ftype)[:20]!r} for {entry_name!r}')
        want = str(entry.get('chksum_sha256') or '')
        if entry.get('chksum_type', 'sha256') != 'sha256' or not _HEX64.match(want):
            raise ImportFailed('invalid_manifest', f'FILES.json has no sha256 checksum for {entry_name!r}')
        got = _resolve(entry_name, regular, links)
        if got is None:
            raise ImportFailed('missing_file', f'{entry_name!r} is listed in FILES.json but not in the archive')
        if got != want:
            raise ImportFailed('checksum_mismatch', f'{entry_name!r} does not match its sha256 in FILES.json')
        listed.add(entry_name)
    unlisted = sorted(n for n in (*regular, *links) if n not in listed and n not in ('MANIFEST.json', 'FILES.json'))
    if unlisted:
        raise ImportFailed('unlisted_file', f'{unlisted[0]!r} is in the archive but not listed in FILES.json')
    requires = None
    if runtime_raw is not None:
        found = _REQUIRES.search(runtime_raw.decode('utf-8', errors='replace'))
        requires = found.group(1).strip() if found else None
    return Collection(
        namespace=namespace,
        name=name,
        version=version,
        metadata=_metadata(info),
        requires_ansible=requires,
        files=len(listed),
        unpacked=unpacked,
    )


def _resolve(name: str, regular: dict[str, str], links: dict[str, str]) -> str | None:
    """The sha256 of ``name``'s content, following symlinks inside the archive."""
    for _ in range(MAX_LINK_HOPS):
        if name in regular:
            return regular[name]
        if name not in links:
            return None
        name = links[name]
    raise ImportFailed('invalid_archive', 'symbolic links nest too deeply')


# ---------------------------------------------------------------------------- the job
@dataclass(frozen=True)
class ImportResult:
    task_id: str
    state: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {'task_id': self.task_id, 'state': self.state, 'detail': self.detail}


def _message(level: str, text: str, now: datetime) -> Document:
    return {'level': level, 'message': text, 'time': now}


async def run_import(db: Database, blobs: BlobStore, task_id: str, *, now: datetime) -> ImportResult:
    task = await galaxy_db.start_import(db, task_id, now=now)
    if task is None:
        return ImportResult(task_id, 'skipped', 'the import is finished or unknown')
    repo_id = str(task['repo_id'])
    expected = galaxy.Artifact(str(task['namespace']), str(task['name']), str(task['version']))
    messages = [_message('INFO', f'Importing {expected.fqcn} {expected.version} ({expected.filename})', now)]

    async def fail(code: str, description: str) -> ImportResult:
        messages.append(_message('ERROR', description, now))
        error = {'code': code, 'description': description}
        await galaxy_db.finish_import(db, task_id, state='failed', messages=messages, error=error, now=now)
        return ImportResult(task_id, 'failed', description)

    repo = await galaxy_db.repository(db, repo_id)
    if repo is None or repo.get('format') != 'galaxy' or repo.get('type') != 'hosted':
        return await fail('repository_gone', 'the repository was deleted or is not a hosted Galaxy repository')
    sha256, size = str(task['sha256']), int(task['size'])
    if blobs.guard is not None:
        await blobs.guard.claim(sha256, size)
    if not await blobs.exists(sha256):
        return await fail('artifact_missing', 'the uploaded artifact is no longer stored: publish it again')
    try:
        found = await asyncio.to_thread(read_collection, blobs.blob_path(sha256), expected)
    except ImportFailed as exc:
        return await fail(exc.code, exc.description)
    messages.append(_message('INFO', f'Checked {found.files} files against FILES.json', now))
    record: Document = {
        'repo_id': repo_id,
        'namespace': found.namespace,
        'name': found.name,
        'version': found.version,
        'version_sort': galaxy.version_sort(found.version),
        'path': expected.path,
        'sha256': sha256,
        'size': size,
        'created': now,
        'requires_ansible': found.requires_ansible,
        'description': found.metadata.get('description'),
        'metadata': found.metadata,
        'import_id': task_id,
        'uploaded_by': task.get('uploaded_by'),
    }
    if not await galaxy_db.insert_version(db, record):
        existing = await galaxy_db.get_version(db, repo_id, found.namespace, found.name, found.version)
        if existing is None or existing.get('import_id') != task_id:
            return await fail(
                'version_exists', f'{expected.fqcn} {expected.version} exists already: versions are immutable'
            )
    await galaxy_db.publish_artifact(db, repo_id, expected.path, sha256=sha256, size=size, now=now)
    messages.append(_message('INFO', f'Published {expected.fqcn} {expected.version}', now))
    await galaxy_db.finish_import(db, task_id, state='completed', messages=messages, error=None, now=now)
    return ImportResult(task_id, 'completed', f'{expected.fqcn} {expected.version}')
