"""``GET /api/v1/system/support-bundle`` (``admin``; SPECIFICATION §4.12 "System", T-O10).

A zip file an administrator downloads from Administration → System and attaches to a support request:

- ``README.txt``: what is in it, and that it should be reviewed before it is shared.
- ``manifest.json``: when, by which version, for which instance, the files.
- ``versions.json``: Waystation, Python, OS and kernel, MongoDB (version, feature compatibility version), Valkey,
  every worker's version, the applied migrations.
- ``settings.json``: the effective environment settings (secrets replaced by ``<redacted>``, credentials removed from
  the MongoDB and Valkey URIs and any other URL), the stored instance settings.
- ``repositories.json`` / ``auth_providers.json``: the configuration with secrets removed (``*_enc`` become
  ``*_set`` booleans), CA bundles reduced to their certificate count and SHA-256 fingerprints.
- ``health.json``: what ``GET /system`` reports, the low-disk guard, workers and queues, upstream health and
  auto-block state of every proxy repository.
- ``jobs.json``: the newest failed, dead and interrupted job runs (the end of their logs) and dead letters.
- ``errors.json``: the newest WARNING/ERROR log records of every server and worker (``errors:recent``).

Every section is read with a time limit and a failure becomes ``{"error": <class>}`` in its file, so a bundle can
be had while MongoDB or Valkey is down. Every value goes through ``scrub`` (secret-looking keys, private keys and URL
credentials; strings, lists and depth bounded) and every file is capped; the zip is built in a thread. Downloading
is audited (``system.support_bundle``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses
import hashlib
import io
import json
import os
import platform
import re
import zipfile
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime
from enum import Enum
from pathlib import PurePath
from typing import Any, Final

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, SecretBytes, SecretStr

from waystation_common.db import jobs as jobs_db
from waystation_common.db.migrations import MIGRATIONS, applied_ids
from waystation_common.logs import mask_userinfo
from waystation_common.recent_errors import read_recent
from waystation_common.settings import ENV_PREFIX, FILE_SUFFIX, Settings, is_secret
from waystation_server import audit
from waystation_server.api import settings as system_api
from waystation_server.api import storage_guard as guard_api
from waystation_server.api import tasks as tasks_api
from waystation_server.api.auth import VERSION
from waystation_server.api.deps import ServicesDep
from waystation_server.api.rbac import PROBLEM
from waystation_server.audit import AuditAction
from waystation_server.db import auth_providers as providers_db
from waystation_server.db import settings as settings_db
from waystation_server.repos.model import RepoType
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate
from waystation_server.services import Services
from waystation_server.stats import repos as repo_stats

router = APIRouter(tags=['admin'])

REDACTED: Final = '<redacted>'
SECTION_TIMEOUT_S: Final = 10.0
MAX_FILE_BYTES: Final = 8 * 1024 * 1024
MAX_STRING: Final = 4000
MAX_ITEMS: Final = 2000
MAX_DEPTH: Final = 12
PROBLEM_RUNS: Final = 100
RUN_LOG_LINES: Final = 20
DEAD_LETTERS: Final = 50
RECENT_ERRORS: Final = 500

# Keys whose values are secrets. ``*_set``/``*_id``/... are facts about them, not secrets.
_SECRET_KEY = re.compile(
    r'passw|secret|token$|tokens$|credential|api_?key|private_?key|privkey|cookie|authori[sz]ation|csrf|_enc$'
    r'|^authmechanismproperties$',
    re.I,
)
_NOT_SECRET = re.compile(r'(_id|_ids|_set|_count|_prefix|_limit|_at)$', re.I)
_PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY')
_PEM_CERT = re.compile(r'-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----', re.S)
URI_SETTINGS: Final = frozenset({'mongo_uri', 'valkey_url'})


# ---------------------------------------------------------------------------- redaction
def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(key)) and not _NOT_SECRET.search(key)


def _set(value: Any) -> bool:
    return value not in (None, '', [], {}, ())


def ca_summary(pem: str) -> dict[str, Any]:
    """A CA bundle as its certificate count and SHA-256 fingerprints (of the DER)."""
    fingerprints = []
    for block in _PEM_CERT.findall(pem)[:100]:
        try:
            der = base64.b64decode(''.join(block.split()), validate=True)
        except binascii.Error, ValueError:
            fingerprints.append('unreadable')
            continue
        fingerprints.append(hashlib.sha256(der).hexdigest())
    return {'certificates': len(fingerprints), 'sha256': fingerprints}


def safe_uri(uri: str) -> str:
    """A connection URI without credentials: the user info and secret-looking query parameters are redacted."""
    scheme, sep, rest = uri.partition('://')
    if not sep:
        return REDACTED if uri else uri
    authority, slash, tail = rest.partition('/')
    query = ''
    if not slash:
        authority, _, query = authority.partition('?')
        tail = ''
    else:
        tail, _, query = tail.partition('?')
    if '@' in authority:
        authority = REDACTED + '@' + authority.rpartition('@')[2]
    out = f'{scheme}://{authority}{slash}{tail}'
    if query:
        params = []
        for pair in query.split('&'):
            name, eq, _value = pair.partition('=')
            params.append(f'{name}={REDACTED}' if eq and is_secret_key(name) else pair)
        out += '?' + '&'.join(params)
    return out


def _clip(text: str) -> str:
    return text if len(text) <= MAX_STRING else text[: MAX_STRING - 1] + '…'


def scrub(value: Any, key: str = '', depth: int = 0) -> Any:
    """A bounded, JSON-safe copy of ``value`` with every secret removed (see the module docstring)."""
    if isinstance(value, SecretStr | SecretBytes):
        return REDACTED if _set(value.get_secret_value()) else None
    if key and is_secret_key(key):
        return REDACTED if _set(value) else None
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Enum):
        return scrub(value.value, key, depth)
    if isinstance(value, str):
        if key == 'ca_pem' and value.strip():
            return ca_summary(value)
        if _PRIVATE_KEY.search(value):
            return REDACTED
        return _clip(mask_userinfo(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return f'<{len(value)} bytes>'
    if isinstance(value, BaseModel):
        return scrub(value.model_dump(mode='python', by_alias=False), key, depth)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return scrub(dataclasses.asdict(value), key, depth)
    if depth >= MAX_DEPTH:
        return '…'
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for i, (raw_key, item) in enumerate(value.items()):
            if i >= MAX_ITEMS:
                out['…'] = f'{len(value) - MAX_ITEMS} more'
                break
            name = str(raw_key)
            if name.endswith('_enc'):  # an encrypted secret: only whether it is set
                out[name.removesuffix('_enc') + '_set'] = _set(item)
                continue
            out[name] = scrub(item, name, depth + 1)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
        result = [scrub(item, key, depth + 1) for item in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            result.append(f'… {len(items) - MAX_ITEMS} more')
        return result
    return scrub(str(value), key, depth)


def environment(settings: Settings, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The effective ``WAYSTATION_*`` settings, secrets redacted (URIs keep everything but their credentials), and
    which secrets were read from ``*_FILE`` files (the variable names only)."""
    env = os.environ if environ is None else environ
    values: dict[str, Any] = {}
    from_files: list[str] = []
    for name, field in Settings.model_fields.items():
        var = f'{ENV_PREFIX}{name.upper()}'
        value = getattr(settings, name)
        if is_secret(field):
            if env.get(var + FILE_SUFFIX):
                from_files.append(var + FILE_SUFFIX)
            if name in URI_SETTINGS and isinstance(value, SecretStr):
                values[var] = safe_uri(value.get_secret_value())
            else:
                values[var] = REDACTED if _set(value) else None
            continue
        values[var] = scrub(value, name)
    return {'variables': values, 'secrets_from_files': from_files}


# ---------------------------------------------------------------------------- sections
async def _guarded(section: Awaitable[Any]) -> Any:
    try:
        async with asyncio.timeout(SECTION_TIMEOUT_S):
            return await section
    except Exception as exc:
        return {'error': system_api._error(exc)}


async def _mongodb_versions(svc: Services) -> dict[str, Any]:
    info = await svc.db.command('buildInfo')
    out: dict[str, Any] = {'version': info.get('version'), 'git_version': info.get('gitVersion')}
    try:
        fcv = await svc.db.client.admin.command({'getParameter': 1, 'featureCompatibilityVersion': 1})
        out['feature_compatibility_version'] = (fcv.get('featureCompatibilityVersion') or {}).get('version')
    except Exception as exc:  # needs a privilege the account may not have
        out['feature_compatibility_version'] = {'error': system_api._error(exc)}
    return out


async def _valkey_versions(svc: Services) -> dict[str, Any]:
    server = await svc.valkey.info('server')
    memory = await svc.valkey.info('memory')
    return {
        'version': server.get('valkey_version') or server.get('redis_version'),
        'mode': server.get('server_mode') or server.get('redis_mode'),
        'uptime_s': server.get('uptime_in_seconds'),
        'used_memory': memory.get('used_memory'),
        'maxmemory': memory.get('maxmemory'),
        'maxmemory_policy': memory.get('maxmemory_policy'),
    }


async def _workers(svc: Services) -> list[dict[str, Any]]:
    overview = await tasks_api.worker_overview(svc)
    return [
        {
            'id': w.id,
            'hostname': w.hostname,
            'version': w.version,
            'state': w.state,
            'leader': w.leader,
            'heartbeat_age_s': w.heartbeat_age_s,
        }
        for w in overview.workers
    ]


async def _migrations(svc: Services) -> dict[str, Any]:
    applied = await applied_ids(svc.db)
    known = sorted(m.id for m in MIGRATIONS)
    return {
        'applied': applied,
        'pending': sorted(set(known) - set(applied)),
        'unknown': sorted(set(applied) - set(known)),
    }


async def versions(svc: Services) -> dict[str, Any]:
    mongodb, valkey, workers, migrations = await asyncio.gather(
        _guarded(_mongodb_versions(svc)),
        _guarded(_valkey_versions(svc)),
        _guarded(_workers(svc)),
        _guarded(_migrations(svc)),
    )
    return {
        'waystation': VERSION,
        'python': platform.python_version(),
        'python_implementation': platform.python_implementation(),
        'os': platform.platform(),
        'kernel': platform.release(),
        'machine': platform.machine(),
        'server_started_at': system_api.STARTED_AT,
        'mongodb': mongodb,
        'valkey': valkey,
        'workers': workers,
        'migrations': migrations,
    }


async def settings_section(svc: Services) -> dict[str, Any]:
    return {
        'environment': environment(svc.settings),
        'instance': await _guarded(settings_db.all_sections(svc.db)),
    }


async def repositories(svc: Services) -> dict[str, Any]:
    repos = await svc.catalog.all()
    statuses = await repo_stats.statuses(svc, repos)
    items = []
    for repo in repos:
        doc = repo.model_dump(mode='python', by_alias=False)
        doc['status'] = statuses.get(repo.id)
        items.append(doc)
    return {'count': len(items), 'items': items}


async def auth_providers(svc: Services) -> dict[str, Any]:
    records = await providers_db.list_all(svc.db)
    return {'count': len(records), 'items': [r.model_dump(mode='python', by_alias=False) for r in records]}


async def _upstreams(svc: Services) -> list[dict[str, Any]]:
    repos = [r for r in await svc.catalog.all() if r.type is RepoType.PROXY and r.proxy is not None]
    states = await repo_stats.health(svc, repos)
    out = []
    for repo in repos:
        assert repo.proxy is not None
        h = states[repo.id]
        out.append(
            {
                'repository': repo.name,
                'format': repo.format,
                'upstream_url': repo.proxy.upstream_url,
                'status': repo_stats.status_of(repo, h),
                'auto_block': repo.proxy.auto_block,
                'health': h,
            }
        )
    return out


async def health(svc: Services) -> dict[str, Any]:
    mongo, valkey, volume, guard, workers, upstreams = await asyncio.gather(
        system_api._mongodb(svc),
        system_api._valkey(svc),
        _guarded(asyncio.to_thread(system_api._volume, svc.settings.data_dir)),
        _guarded(guard_api._state(svc)),
        _guarded(tasks_api.worker_overview(svc)),
        _guarded(_upstreams(svc)),
    )
    total, used, free = volume if isinstance(volume, tuple) else (None, None, None)
    return {
        'components': [mongo, valkey],
        'storage': {
            'path': str(svc.settings.data_dir),
            'total_bytes': total,
            'used_bytes': used,
            'free_bytes': free,
        },
        'low_disk_guard': guard,
        'workers': workers,
        'upstreams': upstreams,
    }


async def jobs(svc: Services) -> dict[str, Any]:
    async def runs() -> list[dict[str, Any]]:
        docs = await jobs_db.recent_problem_runs(svc.db, limit=PROBLEM_RUNS, log_lines=RUN_LOG_LINES)
        return [{**tasks_api._summary_fields(d), 'log_tail': d.get('log') or []} for d in docs]

    async def dead() -> dict[str, Any]:
        letters = await svc.jobs.queue.dead_page(before=None, count=DEAD_LETTERS)
        return {
            'total': await svc.jobs.queue.dead_count(),
            'newest': [tasks_api._dead_view(letter) for letter in letters],
        }

    problem_runs, dead_letters = await asyncio.gather(_guarded(runs()), _guarded(dead()))
    return {'problem_runs': problem_runs, 'dead_letters': dead_letters}


async def errors(svc: Services) -> dict[str, Any]:
    items = await read_recent(svc.valkey, RECENT_ERRORS)
    return {
        'note': 'The newest WARNING and ERROR log records of every server and worker process (at most 1000 kept, '
        'for 7 days); floods of one message are summarised.',
        'count': len(items),
        'items': items,
    }


README = """Waystation support bundle
=========================

Generated {generated_at} by Waystation {version} for the instance "{instance}".

REVIEW THESE FILES BEFORE YOU SHARE THEM. Secrets are removed (passwords, tokens, keys, encrypted credentials,
credentials inside URLs), but the files still name your hosts, repositories, upstream URLs, users of the
authentication providers and what your log records say.

Files
-----
manifest.json         when and by which version this bundle was made; the file list
versions.json         Waystation, Python, OS/kernel, MongoDB (and feature compatibility version), Valkey, workers,
                      applied database migrations
settings.json         the effective WAYSTATION_* environment settings (secrets shown as <redacted>) and the stored
                      instance settings
repositories.json     every repository's configuration (secrets only as *_set flags; CA bundles as fingerprints)
auth_providers.json   LDAP/OIDC provider configuration (secrets only as *_set flags)
health.json           MongoDB/Valkey health, the data volume and low-disk guard, workers and queues, upstream health
                      and auto-block state per proxy repository
jobs.json             the newest failed, dead and interrupted job runs (the end of their logs) and dead letters
errors.json           the newest WARNING/ERROR log records of every server and worker process

A section that could not be read says so: {{"error": "<reason>"}}.
"""


def _slug(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:40] or 'waystation'


def _json(value: Any) -> bytes:
    data = json.dumps(scrub(value), indent=2, ensure_ascii=False, default=str).encode()
    if len(data) > MAX_FILE_BYTES:
        note = {'truncated': True, 'bytes': len(data), 'limit': MAX_FILE_BYTES, 'note': 'this section was too large'}
        data = json.dumps(note, indent=2).encode()
    return data


def build_zip(sections: Mapping[str, Any], manifest: dict[str, Any], readme: str) -> bytes:
    """The bundle (CPU work: run it in a thread). ``manifest`` gains the file list."""
    files = {name: _json(value) for name, value in sections.items()}
    manifest = {**manifest, 'files': [{'name': n, 'bytes': len(b)} for n, b in files.items()]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr('README.txt', readme)
        zf.writestr('manifest.json', _json(manifest))
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


@router.get(
    '/system/support-bundle',
    operation_id='getSupportBundle',
    response_class=Response,
    responses={
        200: {
            'description': 'The support bundle (zip)',
            'content': {'application/zip': {'schema': {'type': 'string', 'format': 'binary'}}},
        },
        403: PROBLEM,
    },
)
@access.requires(Action.ADMIN)
async def get_support_bundle(request: Request, svc: ServicesDep) -> Response:
    principal = await authenticate(request)
    now = datetime.now(UTC)
    instance = (await svc.instance.general()).instance_name
    names = ('versions', 'settings', 'repositories', 'auth_providers', 'health', 'jobs', 'errors')
    results = await asyncio.gather(
        _guarded(versions(svc)),
        _guarded(settings_section(svc)),
        _guarded(repositories(svc)),
        _guarded(auth_providers(svc)),
        _guarded(health(svc)),
        _guarded(jobs(svc)),
        _guarded(errors(svc)),
    )
    sections = {f'{name}.json': result for name, result in zip(names, results, strict=True)}
    manifest = {
        'generated_at': now,
        'generator': f'Waystation {VERSION}',
        'instance_name': instance,
        'generated_by': principal.name,
    }
    readme = README.format(generated_at=now.isoformat(timespec='seconds'), version=VERSION, instance=instance)
    data = await asyncio.to_thread(build_zip, sections, manifest, readme)
    audit.record(
        AuditAction.SYSTEM_SUPPORT_BUNDLE,
        target=audit.target('system', 'support-bundle', 'Support bundle'),
        details={'bytes': len(data), 'files': len(sections) + 2},
    )
    filename = f'waystation-support-{_slug(instance)}-{now.strftime("%Y%m%dT%H%M%SZ")}.zip'
    return Response(
        content=data,
        media_type='application/zip',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
        },
    )
