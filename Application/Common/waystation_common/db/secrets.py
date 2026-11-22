"""Stored ciphertexts and keyed hashes, for the secret-key rotation job (``waystation_worker.rotate``, T-O11,
ARCHITECTURE §7).

``ENCRYPTED_FIELDS`` is the catalogue of every MongoDB field that holds a ``waystation_common.crypto`` ciphertext,
with the associated data its owner encrypts it with (a test compares each entry with the owning code). A field whose
value does not start with the active key's prefix (``ws1.<kid>.``) needs re-encrypting; ``stale_page`` selects only
those, so a run with nothing to do reads nothing. ``replace`` is a compare-and-set on the old ciphertext: a value an
operator changed in the meantime is never overwritten (the change already used the active key).

Keyed hashes (API tokens, TOTP recovery codes) cannot be recomputed without the secret, so the job only reports
which key made them (``hash_kid`` / ``totp_recovery_kid``; absent on records made before T-O11): ``hash_key_report``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from waystation_common.crypto import kid_prefix
from waystation_common.db.client import Database, Document
from waystation_common.db.collections import API_TOKENS, AUTH_PROVIDERS, REPOSITORIES, SESSIONS, USERS, WEBHOOKS

PAGE = 200


@dataclass(frozen=True)
class EncryptedField:
    """One stored ciphertext: ``collection``, dotted ``path``, and its associated data: ``aad_prefix`` + the
    document id, or ``aad_prefix`` alone when ``per_document`` is False."""

    collection: str
    path: str
    aad_prefix: str
    per_document: bool = True

    @property
    def name(self) -> str:
        return f'{self.collection}.{self.path}'

    def aad(self, doc_id: str) -> bytes:
        return (self.aad_prefix + doc_id if self.per_document else self.aad_prefix).encode()


ENCRYPTED_FIELDS: tuple[EncryptedField, ...] = (
    # upstream credentials (waystation_server.repos.model.secret_aad; the worker's upstream client reads them too)
    EncryptedField(REPOSITORIES, 'proxy.auth.secret_enc', 'repo:'),
    # LDAP service bind and OIDC client secret (waystation_server.security.providers.secret_aad)
    EncryptedField(AUTH_PROVIDERS, 'ldap.connection.bind_password_enc', 'auth_provider:'),
    EncryptedField(AUTH_PROVIDERS, 'oidc.client_secret_enc', 'auth_provider:'),
    # webhook HMAC key and custom headers (waystation_server.api.webhooks, waystation_worker.webhooks)
    EncryptedField(WEBHOOKS, 'secret_enc', 'webhook:'),
    EncryptedField(WEBHOOKS, 'headers_enc', 'webhook-headers:'),
    # TOTP secrets, enrolled and pending (waystation_server.security.second_factor.secret_aad)
    EncryptedField(USERS, 'totp_enc', 'user-totp:'),
    EncryptedField(USERS, 'totp_pending_enc', 'user-totp:'),
    # an OIDC session's ID token for RP-initiated logout (waystation_server.oidc_auth.ID_TOKEN_AAD)
    EncryptedField(SESSIONS, 'id_token_enc', 'session-oidc-id-token', per_document=False),
)


def _current(kid: str) -> re.Pattern[str]:
    return re.compile('^' + re.escape(kid_prefix(kid)))


async def stale_page(
    db: Database, field: EncryptedField, active_kid: str, *, after: str | None, limit: int = PAGE
) -> list[tuple[str, str]]:
    """(document id, ciphertext) of values not made with ``active_kid``, in ``_id`` order after ``after``. A value
    that is not a string at all is returned too (the caller reports it as undecryptable)."""
    query: Document = {
        field.path: {'$exists': True, '$ne': None, '$not': _current(active_kid)},
    }
    if after is not None:
        query['_id'] = {'$gt': after}
    cursor = db[field.collection].find(query, {field.path: 1}).sort('_id', 1).limit(limit)
    out: list[tuple[str, str]] = []
    async for doc in cursor:
        value: Any = doc
        for part in field.path.split('.'):
            value = value.get(part) if isinstance(value, dict) else None
        out.append((str(doc['_id']), value if isinstance(value, str) else repr(type(value))))
    return out


async def count_current(db: Database, field: EncryptedField, active_kid: str) -> int:
    """Values already made with the active key."""
    return await db[field.collection].count_documents({field.path: _current(active_kid)})


async def replace(db: Database, field: EncryptedField, doc_id: str, old: str, new: str) -> bool:
    """Store ``new`` only where ``old`` still is (compare-and-set); False when the value changed meanwhile."""
    result = await db[field.collection].update_one({'_id': doc_id, field.path: old}, {'$set': {field.path: new}})
    return result.modified_count == 1


async def _by_kid(db: Database, collection: str, match: Document, kid_field: str) -> dict[str | None, int]:
    pipeline = [{'$match': match}, {'$group': {'_id': f'${kid_field}', 'n': {'$sum': 1}}}]
    return {doc['_id']: int(doc['n']) async for doc in await db[collection].aggregate(pipeline)}


def _summarise(counts: dict[str | None, int], active_kid: str) -> dict[str, int]:
    return {
        'current': counts.get(active_kid, 0),
        'older_key': sum(n for kid, n in counts.items() if kid not in (None, active_kid)),
        'unrecorded': counts.get(None, 0),
    }


async def hash_key_report(db: Database, active_kid: str, *, now: datetime) -> dict[str, dict[str, int]]:
    """Which key hashed the live API tokens and the users' unused recovery codes: ``current`` (the active key),
    ``older_key`` (another key: stops working when that key is removed) and ``unrecorded`` (made before the key was
    recorded, so unknown)."""
    live = {'revoked_at': None, '$or': [{'expires_at': None}, {'expires_at': {'$gt': now}}]}
    tokens = await _by_kid(db, API_TOKENS, live, 'hash_kid')
    recovery = await _by_kid(db, USERS, {'totp_recovery.0': {'$exists': True}}, 'totp_recovery_kid')
    return {
        'api_tokens': _summarise(tokens, active_kid),
        'recovery_codes': _summarise(recovery, active_kid),
    }
