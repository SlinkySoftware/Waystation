"""Secret-key rotation job (T-O11) against real MongoDB/Valkey and a real keys directory: values under the old key
are re-encrypted and still read by the code that owns them with only the new key, current values are untouched, a
value changed meanwhile is not overwritten, undecryptable values are reported (ids, never values) and fail the run
while everything else is rotated, the key files are rewritten and still load, a second run is a no-op, the keyed
hash report, the audit record, the token re-hash flush of the rollup and ``run_task``."""

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio import Redis
from ws_testing import make_settings

from waystation_common import keyfiles
from waystation_common.crypto import Keyring, generate_key, kid_prefix
from waystation_common.db import jobs as jobs_db
from waystation_common.db import secrets as secrets_db
from waystation_common.db import seen
from waystation_common.db.client import Database
from waystation_common.db.collections import (
    API_TOKENS,
    AUDIT,
    AUTH_PROVIDERS,
    JOB_OUTBOX,
    REPOSITORIES,
    SESSIONS,
    USERS,
    WEBHOOKS,
)
from waystation_common.db.schema import ensure_schema
from waystation_common.jobs import JobEnvelope, SecretsRotate
from waystation_common.signing import KeyStore
from waystation_server import oidc_auth
from waystation_server.api import webhooks as webhooks_api
from waystation_server.repos.model import secret_aad as repo_aad
from waystation_server.security import providers, registry_token, second_factor
from waystation_worker import rollup, rotate, run_task
from waystation_worker.jobs import BUILTIN_DEFINITIONS, HANDLERS, JobContext, JobFailedError

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 22, 4, 23, tzinfo=UTC)
OLD, NEW, LOST = generate_key(), generate_key(), generate_key()
OLD_RING = Keyring.from_strings([OLD])
RING = Keyring.from_strings([NEW, OLD])  # the new key put first
NEW_ONLY = Keyring.from_strings([NEW])  # after the old key is removed
LOST_RING = Keyring.from_strings([LOST])  # a key nobody configured any more

UPSTREAM_SECRET = 'upstream-password-value'


def enc(ring: Keyring, value: str, aad: bytes) -> str:
    return ring.encrypt_str(value, aad=aad)


async def seed(db: Database) -> None:
    await db[REPOSITORIES].insert_many(
        [
            {
                '_id': 'r-old',
                'name': 'old',
                'type': 'proxy',
                'proxy': {
                    'auth': {
                        'kind': 'basic',
                        'username': 'u',
                        'secret_enc': enc(OLD_RING, UPSTREAM_SECRET, repo_aad('r-old')),
                    }
                },
            },
            {
                '_id': 'r-new',
                'name': 'new',
                'type': 'proxy',
                'proxy': {
                    'auth': {
                        'kind': 'basic',
                        'username': 'u',
                        'secret_enc': enc(RING, 'already-current', repo_aad('r-new')),
                    }
                },
            },
            {
                '_id': 'r-lost',
                'name': 'lost',
                'type': 'proxy',
                'proxy': {
                    'auth': {'kind': 'basic', 'username': 'u', 'secret_enc': enc(LOST_RING, 'gone', repo_aad('r-lost'))}
                },
            },
            {'_id': 'r-none', 'name': 'none', 'type': 'proxy', 'proxy': {'auth': {'kind': 'none', 'secret_enc': None}}},
            {'_id': 'r-hosted', 'name': 'hosted', 'type': 'hosted'},
        ]
    )
    await db[AUTH_PROVIDERS].insert_many(
        [
            {
                '_id': 'p-ldap',
                'kind': 'ldap',
                'ldap': {
                    'connection': {
                        'bind_password_enc': enc(OLD_RING, 'bind-password-value', providers.secret_aad('p-ldap'))
                    }
                },
            },
            {
                '_id': 'p-oidc',
                'kind': 'oidc',
                'oidc': {'client_secret_enc': enc(OLD_RING, 'client-secret-value', providers.secret_aad('p-oidc'))},
            },
        ]
    )
    await db[WEBHOOKS].insert_one(
        {
            '_id': 'w1',
            'name': 'hook',
            'secret_enc': enc(OLD_RING, 'hmac-key-value', webhooks_api.secret_aad('w1')),
            'headers_enc': enc(OLD_RING, json.dumps({'X-Api': 'header-value'}), webhooks_api.headers_aad('w1')),
        }
    )
    await db[USERS].insert_many(
        [
            {
                '_id': 'u1',
                'username': 'u1',
                'totp_enc': enc(OLD_RING, 'TOTPSECRETVALUE1', second_factor.secret_aad('u1')),
                'totp_recovery': ['h1', 'h2'],
                'totp_recovery_kid': OLD_RING.active_kid,
            },
            {
                '_id': 'u2',
                'username': 'u2',
                'totp_enc': None,
                'totp_pending_enc': enc(OLD_RING, 'TOTPSECRETVALUE2', second_factor.secret_aad('u2')),
            },
            {'_id': 'u3', 'username': 'u3', 'totp_recovery': ['h3']},  # codes made before T-O11: key unrecorded
            {'_id': 'u4', 'username': 'u4', 'totp_recovery': [], 'totp_recovery_kid': OLD_RING.active_kid},  # none
        ]
    )
    await db[SESSIONS].insert_many(
        [
            {'_id': 's1', 'user_id': 'u1', 'id_token_enc': enc(OLD_RING, 'id.token.value', oidc_auth.ID_TOKEN_AAD)},
            {'_id': 's2', 'user_id': 'u1'},
        ]
    )
    live = {'revoked_at': None, 'expires_at': None}
    await db[API_TOKENS].insert_many(
        [
            {'_id': 't-old', 'hash': 'a' * 64, 'hash_kid': OLD_RING.active_kid, **live},
            {'_id': 't-legacy', 'hash': 'b' * 64, **live},
            {'_id': 't-new', 'hash': 'c' * 64, 'hash_kid': RING.active_kid, **live},
            {'_id': 't-revoked', 'hash': 'd' * 64, 'hash_kid': OLD_RING.active_kid, 'revoked_at': NOW},
            {'_id': 't-expired', 'hash': 'e' * 64, 'revoked_at': None, 'expires_at': NOW - timedelta(days=1)},
        ]
    )


@pytest.fixture
async def db(mongo_db: Database) -> Database:
    await ensure_schema(mongo_db)
    await seed(mongo_db)
    return mongo_db


@pytest.fixture(scope='module')
def signing_key_text(tmp_path_factory: pytest.TempPathFactory) -> str:
    root = tmp_path_factory.mktemp('keys')
    KeyStore(root, OLD_RING).load_or_create('repo1', 'Waystation repository test (repo1)')
    return (root / 'repo1.json').read_text()


@pytest.fixture
def keys_dir(tmp_path: Path, signing_key_text: str) -> Path:
    root = tmp_path / 'data' / 'keys'
    root.mkdir(parents=True)
    (root / 'repo1.json').write_text(signing_key_text)
    (root / 'repo1.json').chmod(0o600)
    registry_token._load_or_create(root / keyfiles.REGISTRY_TOKEN_KEY_FILE, OLD_RING)
    return root


def file_modes(root: Path) -> dict[str, int]:
    return {path.name: stat.S_IMODE(path.stat().st_mode) for path in root.iterdir()}


async def value(db: Database, collection: str, doc_id: str, path: str) -> Any:
    doc = await db[collection].find_one({'_id': doc_id})
    for part in path.split('.'):
        doc = doc.get(part) if isinstance(doc, dict) else None
    return doc


async def test_rotation_re_encrypts_everything_under_older_keys(db: Database, keys_dir: Path) -> None:
    before_current = await value(db, REPOSITORIES, 'r-new', 'proxy.auth.secret_enc')
    lost_before = await value(db, REPOSITORIES, 'r-lost', 'proxy.auth.secret_enc')
    signer_before = registry_token._load_or_create(keys_dir / keyfiles.REGISTRY_TOKEN_KEY_FILE, OLD_RING)
    key_before = KeyStore(keys_dir, OLD_RING).load('repo1')
    lines: list[str] = []
    progress: list[tuple[int, int | None, str | None]] = []

    async def log_line(line: str) -> None:
        lines.append(line)

    async def on_progress(done: int, total: int | None, message: str | None) -> None:
        progress.append((done, total, message))

    result = await rotate.rotate(db, RING, keys_dir, now=NOW, progress=on_progress, log_line=log_line)
    out = result.as_dict()
    assert (out['active_kid'], out['keys']) == (RING.active_kid, 2)
    stores = out['stores']
    assert stores['repositories.proxy.auth.secret_enc'] == {
        'rotated': 1,
        'already_current': 1,
        'conflicts': 0,
        'undecryptable': 1,
        'undecryptable_ids': ['r-lost'],
    }
    for name in (
        'auth_providers.ldap.connection.bind_password_enc',
        'auth_providers.oidc.client_secret_enc',
        'webhooks.secret_enc',
        'webhooks.headers_enc',
        'users.totp_enc',
        'users.totp_pending_enc',
        'sessions.id_token_enc',
    ):
        assert stores[name] == {'rotated': 1, 'already_current': 0, 'conflicts': 0, 'undecryptable': 0}, name
    assert stores[rotate.FILES] == {'rotated': 2, 'already_current': 0, 'conflicts': 0, 'undecryptable': 0}
    assert (out['rotated'], out['undecryptable'], out['conflicts']) == (10, 1, 0)
    assert out['hashes'] == {
        'api_tokens': {'current': 1, 'older_key': 1, 'unrecorded': 1},
        'recovery_codes': {'current': 0, 'older_key': 1, 'unrecorded': 1},
    }
    assert progress and lines == [
        'repositories.proxy.auth.secret_enc: 1 values no configured key decrypts (ids r-lost)'
    ]
    assert UPSTREAM_SECRET not in json.dumps(out)

    # The owners read every value with the new key alone.
    assert (
        NEW_ONLY.decrypt_str(await value(db, REPOSITORIES, 'r-old', 'proxy.auth.secret_enc'), aad=repo_aad('r-old'))
        == UPSTREAM_SECRET
    )
    assert await value(db, REPOSITORIES, 'r-new', 'proxy.auth.secret_enc') == before_current  # untouched
    assert await value(db, REPOSITORIES, 'r-lost', 'proxy.auth.secret_enc') == lost_before  # left as it was
    ldap = await value(db, AUTH_PROVIDERS, 'p-ldap', 'ldap.connection.bind_password_enc')
    assert NEW_ONLY.decrypt_str(ldap, aad=providers.secret_aad('p-ldap')) == 'bind-password-value'
    oidc = await value(db, AUTH_PROVIDERS, 'p-oidc', 'oidc.client_secret_enc')
    assert NEW_ONLY.decrypt_str(oidc, aad=providers.secret_aad('p-oidc')) == 'client-secret-value'
    hook = await db[WEBHOOKS].find_one({'_id': 'w1'})
    assert hook is not None
    assert NEW_ONLY.decrypt_str(hook['secret_enc'], aad=webhooks_api.secret_aad('w1')) == 'hmac-key-value'
    assert json.loads(NEW_ONLY.decrypt_str(hook['headers_enc'], aad=webhooks_api.headers_aad('w1'))) == {
        'X-Api': 'header-value'
    }
    totp = await value(db, USERS, 'u1', 'totp_enc')
    assert NEW_ONLY.decrypt_str(totp, aad=second_factor.secret_aad('u1')) == 'TOTPSECRETVALUE1'
    pending = await value(db, USERS, 'u2', 'totp_pending_enc')
    assert NEW_ONLY.decrypt_str(pending, aad=second_factor.secret_aad('u2')) == 'TOTPSECRETVALUE2'
    assert await value(db, USERS, 'u2', 'totp_enc') is None
    token = await value(db, SESSIONS, 's1', 'id_token_enc')
    assert NEW_ONLY.decrypt_str(token, aad=oidc_auth.ID_TOKEN_AAD) == 'id.token.value'

    # Key files: rewritten (mode kept), loaded by the server and worker code with the new key alone, same keys.
    signer_after = registry_token._load_or_create(keys_dir / keyfiles.REGISTRY_TOKEN_KEY_FILE, NEW_ONLY)
    assert signer_after.public_key().public_bytes_raw() == signer_before.public_key().public_bytes_raw()
    assert KeyStore(keys_dir, NEW_ONLY).load('repo1').public.fingerprint_hex == key_before.public.fingerprint_hex
    assert file_modes(keys_dir) == {keyfiles.REGISTRY_TOKEN_KEY_FILE: 0o600, 'repo1.json': 0o600}  # no temp files

    # Second run: nothing left to do (the lost value is still reported).
    again = (await rotate.rotate(db, RING, keys_dir, now=NOW)).as_dict()
    assert (again['rotated'], again['conflicts'], again['undecryptable']) == (0, 0, 1)
    assert again['already_current'] == 11
    assert again['stores'][rotate.FILES]['already_current'] == 2


async def test_a_value_changed_meanwhile_is_not_overwritten(
    db: Database, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = secrets_db.replace
    edited = enc(RING, 'operator-set-this', webhooks_api.secret_aad('w1'))

    async def racing(db_: Database, spec: secrets_db.EncryptedField, doc_id: str, old: str, new: str) -> bool:
        if spec.name == 'webhooks.secret_enc':  # an operator saves a new secret between the read and the write
            await db[WEBHOOKS].update_one({'_id': doc_id}, {'$set': {'secret_enc': edited}})
        return await real_replace(db_, spec, doc_id, old, new)

    monkeypatch.setattr(secrets_db, 'replace', racing)
    out = (await rotate.rotate(db, RING, keys_dir, now=NOW)).as_dict()
    assert out['stores']['webhooks.secret_enc'] == {
        'rotated': 0,
        'already_current': 0,
        'conflicts': 1,
        'undecryptable': 0,
    }
    assert out['stores']['webhooks.headers_enc']['rotated'] == 1
    assert await value(db, WEBHOOKS, 'w1', 'secret_enc') == edited
    assert not await secrets_db.replace(db, secrets_db.ENCRYPTED_FIELDS[3], 'w1', 'not-the-value', 'x')


async def test_handler_fails_the_run_reports_and_audits(db: Database, valkey: Redis, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, secret_keys=f'{NEW},{OLD}')
    (tmp_path / 'data' / 'keys').mkdir()
    registry_token._load_or_create(tmp_path / 'data' / 'keys' / keyfiles.REGISTRY_TOKEN_KEY_FILE, OLD_RING)
    job = SecretsRotate()
    env = JobEnvelope.for_payload(job)
    ctx = JobContext(job=env, run_id='r1', attempt=1, worker_id='w', db=db, valkey=valkey, settings=settings)
    with pytest.raises(JobFailedError) as failed:
        await HANDLERS[SecretsRotate.kind](job, ctx)
    assert failed.value.retry is False
    assert 'repositories.proxy.auth.secret_enc (1)' in str(failed.value)
    assert failed.value.result is not None and failed.value.result['rotated'] == 9
    assert failed.value.result['stores'][rotate.FILES]['rotated'] == 1
    (record,) = [doc async for doc in db[AUDIT].find({'action': 'secrets.rotate'})]
    assert (record['outcome'], record['reason'], record['principal']['kind']) == ('failure', 'undecryptable', 'system')
    assert record['details']['rotated'] == 9 and record['details']['undecryptable'] == 1
    assert UPSTREAM_SECRET not in json.dumps(record, default=str)

    # The lost credential is re-entered (with the active key): the next run succeeds, audits nothing new.
    await db[REPOSITORIES].update_one(
        {'_id': 'r-lost'}, {'$set': {'proxy.auth.secret_enc': enc(RING, 'fixed', repo_aad('r-lost'))}}
    )
    result = await HANDLERS[SecretsRotate.kind](job, ctx)
    assert result is not None and (result['rotated'], result['undecryptable']) == (0, 0)
    assert await db[AUDIT].count_documents({'action': 'secrets.rotate'}) == 1  # quiet runs are not recorded

    without_keys = make_settings(tmp_path, secret_keys=[])
    ctx = JobContext(job=env, run_id='r2', attempt=1, worker_id='w', db=db, valkey=valkey, settings=without_keys)
    assert 'skipped' in (await HANDLERS[SecretsRotate.kind](job, ctx) or {})
    with pytest.raises(RuntimeError):
        await HANDLERS[SecretsRotate.kind](
            job, JobContext(job=env, run_id='r3', attempt=1, worker_id='w', db=db, valkey=valkey)
        )


async def test_stale_values_are_selected_by_key_prefix(db: Database) -> None:
    spec = secrets_db.ENCRYPTED_FIELDS[0]
    page = await secrets_db.stale_page(db, spec, RING.active_kid, after=None)
    assert [doc_id for doc_id, _ in page] == ['r-lost', 'r-old']
    assert all(not sealed.startswith(kid_prefix(RING.active_kid)) for _, sealed in page)
    assert await secrets_db.stale_page(db, spec, RING.active_kid, after='r-lost') == page[1:]
    await db[REPOSITORIES].update_one({'_id': 'r-hosted'}, {'$set': {'proxy': {'auth': {'secret_enc': 42}}}})
    odd = await secrets_db.stale_page(db, spec, RING.active_kid, after='r-a')
    assert ('r-hosted', "<class 'int'>") in odd  # not a ciphertext at all: reported as undecryptable


async def test_rollup_flushes_queued_token_rehashes(db: Database, valkey: Redis) -> None:
    await valkey.hset(seen.REHASH_TOKENS, 't-old', f'{"a" * 64} {"f" * 64} {RING.active_kid}')
    await rollup.flush_last_seen(valkey, db)
    doc = await db[API_TOKENS].find_one({'_id': 't-old'})
    assert doc is not None and (doc['hash'], doc['hash_kid']) == ('f' * 64, RING.active_kid)
    assert not await valkey.exists(seen.REHASH_TOKENS)


async def test_run_task_runs_secrets_rotate(db: Database) -> None:
    await jobs_db.ensure_definitions(db, BUILTIN_DEFINITIONS)
    queued = await run_task.run(db, 'secrets.rotate', wait=False, timeout_s=1)
    assert queued['status'] == 'queued'
    outbox = await db[JOB_OUTBOX].find_one({'job.definition_id': 'secrets.rotate'})
    assert outbox is not None and outbox['job']['kind'] == SecretsRotate.kind
    assert outbox['job']['queue'] == 'maintenance' and outbox['job']['trigger'] == 'manual'
