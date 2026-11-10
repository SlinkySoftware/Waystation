"""The ``init`` one-shot job (SPECIFICATION §8.1): ``python -m waystation_server.init [--recursive-perms]``.

Steps, each idempotent (a re-run changes nothing):

1. Data dir layout, ownership and modes (``waystation_common.datadir``). The container starts as root with only
   CHOWN, FOWNER, DAC_OVERRIDE, SETUID and SETGID; after this step it drops to ``WAYSTATION_UID:GID``.
2. Replica set initiation when ``WAYSTATION_MONGO_REPLSET_INIT=true`` (bundled MongoDB).
3. Collections and indexes (``waystation_common.db.schema``), then pending migrations (``db.migrations``).
4. The first administrator when there are no users (``waystation_server.bootstrap``).
5. The provisioning file when ``WAYSTATION_PROVISION_FILE`` is set (``waystation_server.provisioning``): applied
   idempotently; an invalid file or a failed change fails ``init`` (exit 1), so the server does not start on a
   configuration other than the declared one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from waystation_common.datadir import drop_privileges, prepare_data_dir
from waystation_common.db.client import Client, create_client, database
from waystation_common.db.migrations import apply_migrations
from waystation_common.db.replset import ensure_replica_set
from waystation_common.db.schema import ensure_schema
from waystation_common.logs import configure_logging
from waystation_common.settings import Settings, get_settings
from waystation_server.bootstrap import banner, ensure_bootstrap_admin
from waystation_server.provisioning import runner as provisioning

class ProvisioningFailed(Exception):
    """The provisioning file is invalid or a change failed (details logged)."""

log = logging.getLogger('waystation.init')


async def run(settings: Settings, *, recursive_perms: bool = False) -> None:
    report = await asyncio.to_thread(
        prepare_data_dir, settings.data_dir, settings.uid, settings.gid, recursive=recursive_perms
    )
    log.info(
        'data dir ready',
        extra={'paths_created': len(report.created), 'paths_fixed': len(report.fixed), 'walked': report.walked},
    )
    drop_privileges(settings.uid, settings.gid)
    if settings.mongo_replset_init:
        initiated = await ensure_replica_set(settings.mongo_uri.get_secret_value())
        log.info('replica set ready', extra={'initiated': initiated})
    client = create_client(settings, app_name='waystation-init')
    try:
        db = database(client, settings)
        schema = await ensure_schema(db)
        log.info(
            'schema ready',
            extra={
                'collections_created': schema.created_collections,
                'indexes_created': schema.created_indexes,
                'conflicts': schema.conflicts,
            },
        )
        applied = await apply_migrations(db)
        log.info('migrations applied', extra={'migrations': applied})
        result = await ensure_bootstrap_admin(db, settings)
        if result.generated_password is not None:
            # Printed once, straight to stderr: never through the (JSON, shipped) logger.
            sys.stderr.write(banner(result.generated_password))
            sys.stderr.flush()
        if settings.provision_file is not None:
            await _provision(settings, client)
    finally:
        await client.close()


async def _provision(settings: Settings, client: Client) -> None:
    assert settings.provision_file is not None
    result = await provisioning.run(settings, settings.provision_file, apply=True, by='init', mongo_client=client)
    summary = result.summary.model_dump()
    if not result.valid:
        for problem in result.errors:
            log.error('provisioning file invalid', extra={'field': problem.field, 'problem': problem.message})
        raise ProvisioningFailed('the provisioning file is invalid: nothing was applied')
    for item in result.items:
        if item.outcome == 'failed':
            log.error('provisioning change failed', extra={'kind': item.kind, 'item': item.name, 'error': item.error})
        elif item.outcome == 'applied':
            log.info('provisioning change applied', extra={'kind': item.kind, 'item': item.name, 'action': item.action})
    if result.summary.failed:
        raise ProvisioningFailed(f'{result.summary.failed} provisioning change(s) failed')
    log.info('provisioning file applied', extra={'sha256': result.sha256, **summary})


def main(argv: list[str]) -> int:  # pragma: no cover - process entry point
    os.umask(0o027)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    try:
        asyncio.run(run(settings, recursive_perms='--recursive-perms' in argv))
    except ProvisioningFailed as exc:
        log.error('init failed', extra={'error': str(exc)})
        return 1
    log.info('init complete')
    return 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
