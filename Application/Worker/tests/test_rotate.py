"""Secret-key rotation (T-O11) without services: the catalogue of stored ciphertexts matches the code that owns each
value (same associated data), and no module encrypts with the keyring without being accounted for here."""

import re
from pathlib import Path

from waystation_common import keyfiles, signing
from waystation_common.db import secrets as secrets_db
from waystation_common.jobs import SecretsRotate
from waystation_server import oidc_auth
from waystation_server.api import webhooks as webhooks_api
from waystation_server.repos import model as repos_model
from waystation_server.security import providers, registry_token, second_factor
from waystation_worker import rotate, upstream
from waystation_worker import webhooks as worker_webhooks
from waystation_worker.jobs import BUILTIN_DEFINITIONS, HANDLERS

APPLICATION = Path(__file__).resolve().parents[2]
DOC_ID = '01JTESTDOCUMENTID'

OWNERS = {
    'repositories.proxy.auth.secret_enc': [repos_model.secret_aad, upstream.secret_aad],
    'auth_providers.ldap.connection.bind_password_enc': [providers.secret_aad],
    'auth_providers.oidc.client_secret_enc': [providers.secret_aad],
    'webhooks.secret_enc': [webhooks_api.secret_aad, worker_webhooks.secret_aad],
    'webhooks.headers_enc': [webhooks_api.headers_aad, worker_webhooks.headers_aad],
    'users.totp_enc': [second_factor.secret_aad],
    'users.totp_pending_enc': [second_factor.secret_aad],
    'sessions.id_token_enc': [lambda _doc_id: oidc_auth.ID_TOKEN_AAD],
}

# Every module that encrypts with the keyring, and where its ciphertext lives.
ENCRYPTING_MODULES = {
    'Server/waystation_server/repos/service.py': 'repositories.proxy.auth.secret_enc',
    'Server/waystation_server/api/auth_providers.py': 'auth_providers.*_enc',
    'Server/waystation_server/api/webhooks.py': 'webhooks.secret_enc / headers_enc',
    'Server/waystation_server/security/second_factor.py': 'users.totp_enc / totp_pending_enc',
    'Server/waystation_server/oidc_auth.py': 'sessions.id_token_enc',
    'Server/waystation_server/security/registry_token.py': 'keys/registry-token.key',
    'Common/waystation_common/signing.py': 'keys/<repo id>.json',
    'Common/waystation_common/keyfiles.py': 'keys/ (re-encryption)',
    'Worker/waystation_worker/rotate.py': 'the rotation itself',
    # Valkey uptok:* entries: encrypted, expire within an hour; never rotated (they are fetched again).
    'Server/waystation_server/formats/docker/upstream_auth.py': 'Valkey uptok:* (ephemeral)',
}
_ENCRYPT = re.compile(r'\.encrypt(_str)?\(')


def test_catalogue_matches_the_owning_code() -> None:
    fields = {spec.name: spec for spec in secrets_db.ENCRYPTED_FIELDS}
    assert set(fields) == set(OWNERS)
    for name, owners in OWNERS.items():
        for owner in owners:
            assert fields[name].aad(DOC_ID) == owner(DOC_ID), name
    assert registry_token.KEY_AAD == keyfiles.REGISTRY_TOKEN_KEY_AAD
    assert registry_token.KEY_FILE == keyfiles.REGISTRY_TOKEN_KEY_FILE
    assert signing.key_aad(DOC_ID) == f'signing-key:{DOC_ID}'.encode()


def test_every_module_that_encrypts_is_accounted_for() -> None:
    found = set()
    for package in ('Common/waystation_common', 'Server/waystation_server', 'Worker/waystation_worker'):
        for path in (APPLICATION / package).rglob('*.py'):
            if path.name != 'crypto.py' and _ENCRYPT.search(path.read_text(encoding='utf-8')):
                found.add(path.relative_to(APPLICATION).as_posix())
    assert found == set(ENCRYPTING_MODULES), 'a new encrypted value: add it to ENCRYPTED_FIELDS or keyfiles'


def test_job_is_registered_and_scheduled_daily() -> None:
    assert SecretsRotate.kind in HANDLERS
    assert SecretsRotate().dedupe_key() == 'secrets.rotate' and SecretsRotate.queue == 'maintenance'
    (definition,) = [d for d in BUILTIN_DEFINITIONS if d.id == 'secrets.rotate']
    assert definition.kind == SecretsRotate.kind and definition.schedule == '23 4 * * *'


def test_result_shape_and_summary() -> None:
    result = rotate.RotationResult(active_kid='abcd1234', keys=2)
    counts = rotate.StoreCounts(rotated=2, already_current=1)
    for n in range(rotate.MAX_IDS + 5):
        counts.failed(f'id-{n}')
    counts.failed(None)
    result.stores['repositories.proxy.auth.secret_enc'] = counts
    result.hashes = {
        'api_tokens': {'current': 1, 'older_key': 2, 'unrecorded': 3},
        'recovery_codes': {'current': 0, 'older_key': 0, 'unrecorded': 1},
    }
    out = result.as_dict()
    assert (out['rotated'], out['undecryptable'], out['keys']) == (2, rotate.MAX_IDS + 6, 2)
    assert len(out['stores']['repositories.proxy.auth.secret_enc']['undecryptable_ids']) == rotate.MAX_IDS
    assert 'undecryptable_ids' not in rotate.StoreCounts().as_dict()
    assert '5 API tokens, 1 users' in result.summary()
