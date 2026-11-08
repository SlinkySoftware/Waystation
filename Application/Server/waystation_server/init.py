"""The ``init`` one-shot job (SPECIFICATION §8.1): ``python -m waystation_server.init [--recursive-perms]``.

Steps, each idempotent (a re-run changes nothing):

1. Data dir layout, ownership and modes (``waystation_common.datadir``). The container starts as root with only
   CHOWN, FOWNER, DAC_OVERRIDE, SETUID and SETGID; after this step it drops to ``WAYSTATION_UID:GID``.
2. Replica set initiation when ``WAYSTATION_MONGO_REPLSET_INIT=true`` (bundled MongoDB).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from waystation_common.datadir import drop_privileges, prepare_data_dir
from waystation_common.db.replset import ensure_replica_set
from waystation_common.logs import configure_logging
from waystation_common.settings import Settings, get_settings

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


def main(argv: list[str]) -> int:  # pragma: no cover - process entry point
    os.umask(0o027)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    asyncio.run(run(settings, recursive_perms='--recursive-perms' in argv))
    log.info('init complete')
    return 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
