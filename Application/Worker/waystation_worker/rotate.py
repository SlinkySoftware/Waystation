"""Secret-key rotation (T-O11, ARCHITECTURE §7; operator procedure: Documents/Operations/SECRET_KEY_ROTATION.md).

``WAYSTATION_SECRET_KEYS``: the first key encrypts, every key decrypts. After a new key is put first, the job
``secrets.rotate`` re-encrypts with it everything stored under an older key, so the older key can be removed:

- every MongoDB field in ``waystation_common.db.secrets.ENCRYPTED_FIELDS`` (upstream credentials, LDAP/OIDC secrets,
  webhook secrets and headers, TOTP secrets, OIDC ID tokens of sessions). Only values whose ciphertext does not name
  the active key are read, so a run with nothing to do is a few empty queries. Each value is decrypted with the
  keyring, encrypted with the active key and written with a compare-and-set on the old ciphertext: a value changed
  meanwhile (it was then encrypted with the active key already) is counted as a ``conflict`` and left alone;
- the files in ``keys/`` (hosted apt/yum signing keys, the registry token signing key): rewritten atomically after the
  new content is checked to decrypt (``waystation_common.keyfiles``).

A value no configured key decrypts is counted as ``undecryptable`` (with the document ids, never a value) and logged
at ERROR; everything else is still rotated, then the run fails (dead-lettered at once: retrying cannot help).

Keyed hashes cannot be re-hashed without their secret: the result reports how many live API tokens and unused
recovery codes were hashed with an older key (``waystation_common.db.secrets.hash_key_report``). API tokens move to
the active key by themselves when they are used (``waystation_server.security.tokens``). Not stored, so not rotated:
the shared upstream registry tokens in Valkey (``uptok:*``, encrypted, expire within an hour) and download grants
(valid 5 minutes).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from waystation_common import keyfiles
from waystation_common.audit import SYSTEM, AuditAction, AuditEvent, AuditTarget, AuditWriter, Outcome
from waystation_common.crypto import Keyring
from waystation_common.db import audit as audit_db
from waystation_common.db import secrets as secrets_db
from waystation_common.db.client import Database
from waystation_common.errors import CryptoError

log = logging.getLogger('waystation.worker.rotate')

FILES = 'keys/'  # the store name of the data directory's key files in results
MAX_IDS = 20  # undecryptable ids reported per store

Progress = Callable[[int, int | None, str | None], Awaitable[None]]
LogLine = Callable[[str], Awaitable[None]]


@dataclass
class StoreCounts:
    rotated: int = 0
    already_current: int = 0
    conflicts: int = 0
    undecryptable: int = 0
    undecryptable_ids: list[str] = field(default_factory=list)

    def failed(self, item_id: str | None) -> None:
        self.undecryptable += 1
        if item_id is not None and len(self.undecryptable_ids) < MAX_IDS:
            self.undecryptable_ids.append(item_id)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            'rotated': self.rotated,
            'already_current': self.already_current,
            'conflicts': self.conflicts,
            'undecryptable': self.undecryptable,
        }
        if self.undecryptable_ids:
            out['undecryptable_ids'] = list(self.undecryptable_ids)
        return out


@dataclass
class RotationResult:
    active_kid: str
    keys: int
    stores: dict[str, StoreCounts] = field(default_factory=dict)
    hashes: dict[str, dict[str, int]] = field(default_factory=dict)

    def total(self, name: str) -> int:
        return sum(int(getattr(counts, name)) for counts in self.stores.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            'active_kid': self.active_kid,
            'keys': self.keys,
            'rotated': self.total('rotated'),
            'already_current': self.total('already_current'),
            'conflicts': self.total('conflicts'),
            'undecryptable': self.total('undecryptable'),
            'stores': {name: counts.as_dict() for name, counts in self.stores.items()},
            'hashes': self.hashes,
        }

    def summary(self) -> str:
        older = {name: report['older_key'] + report['unrecorded'] for name, report in self.hashes.items()}
        return (
            f'active key {self.active_kid} ({self.keys} configured): {self.total("rotated")} values re-encrypted, '
            f'{self.total("already_current")} already current, {self.total("conflicts")} changed meanwhile, '
            f'{self.total("undecryptable")} undecryptable; still hashed with an older or unrecorded key: '
            f"{older.get('api_tokens', 0)} API tokens, {older.get('recovery_codes', 0)} users' recovery codes"
        )


async def _nothing(*_args: Any) -> None:
    return None


async def rotate_fields(
    db: Database, keyring: Keyring, result: RotationResult, *, progress: Progress, log_line: LogLine
) -> None:
    kid = keyring.active_kid
    done = 0
    for spec in secrets_db.ENCRYPTED_FIELDS:
        counts = StoreCounts(already_current=await secrets_db.count_current(db, spec, kid))
        after: str | None = None
        while page := await secrets_db.stale_page(db, spec, kid, after=after):
            for doc_id, sealed in page:
                after = doc_id
                try:
                    plaintext = keyring.decrypt(sealed, aad=spec.aad(doc_id))
                except CryptoError:
                    counts.failed(doc_id if spec.per_document else None)
                    continue
                fresh = keyring.encrypt(plaintext, aad=spec.aad(doc_id))
                if await secrets_db.replace(db, spec, doc_id, sealed, fresh):
                    counts.rotated += 1
                else:
                    counts.conflicts += 1
            done += len(page)
            await progress(done, None, spec.name)
        result.stores[spec.name] = counts
        if counts.undecryptable:
            log.error(
                'stored secrets no configured key can decrypt',
                extra={'store': spec.name, 'count': counts.undecryptable, 'ids': counts.undecryptable_ids},
            )
            await log_line(
                f'{spec.name}: {counts.undecryptable} values no configured key decrypts'
                + (f' (ids {", ".join(counts.undecryptable_ids)})' if counts.undecryptable_ids else '')
            )


async def rotate_files(keys_dir: Path, keyring: Keyring, result: RotationResult, *, log_line: LogLine) -> None:
    counts = StoreCounts()
    for path in await asyncio.to_thread(keyfiles.key_files, keys_dir):
        try:
            rotated = await asyncio.to_thread(keyfiles.rotate_file, path, keyring)
        except FileNotFoundError:  # removed meanwhile (a deleted repository's key)
            continue
        except CryptoError:
            counts.failed(path.name)
            continue
        if rotated:
            counts.rotated += 1
        else:
            counts.already_current += 1
    result.stores[FILES] = counts
    if counts.undecryptable:
        log.error(
            'key files no configured key can decrypt',
            extra={'store': FILES, 'count': counts.undecryptable, 'ids': counts.undecryptable_ids},
        )
        await log_line(f'{FILES}: {counts.undecryptable} files no configured key decrypts: {counts.undecryptable_ids}')


async def rotate(
    db: Database,
    keyring: Keyring,
    keys_dir: Path,
    *,
    now: datetime,
    progress: Progress = _nothing,
    log_line: LogLine = _nothing,
) -> RotationResult:
    """Re-encrypt every stored value and key file not made with the active key; report the keyed hashes."""
    result = RotationResult(active_kid=keyring.active_kid, keys=len(keyring.kids))
    await rotate_fields(db, keyring, result, progress=progress, log_line=log_line)
    await rotate_files(keys_dir, keyring, result, log_line=log_line)
    result.hashes = await secrets_db.hash_key_report(db, keyring.active_kid, now=now)
    return result


async def audit(db: Database, result: RotationResult, *, retention_days: int) -> None:
    """One audit record (actor ``system``, counts only) when the run changed something or found undecryptable
    values; quiet runs are not recorded."""
    rotated, undecryptable = result.total('rotated'), result.total('undecryptable')
    if not rotated and not undecryptable:
        return
    writer = AuditWriter(audit_db.inserter(db), retention_days=retention_days)
    writer.start()
    writer.record(
        AuditEvent(
            action=AuditAction.SECRETS_ROTATE,
            outcome=Outcome.FAILURE if undecryptable else Outcome.SUCCESS,
            reason='undecryptable' if undecryptable else None,
            actor=SYSTEM,
            target=AuditTarget(kind='secret_keys', id=result.active_kid, name='WAYSTATION_SECRET_KEYS'),
            details={
                'active_kid': result.active_kid,
                'keys': result.keys,
                'rotated': rotated,
                'conflicts': result.total('conflicts'),
                'undecryptable': undecryptable,
                'stores': {
                    name: {'rotated': c.rotated, 'undecryptable': c.undecryptable}
                    for name, c in result.stores.items()
                    if c.rotated or c.undecryptable
                },
            },
        )
    )
    await writer.aclose()
