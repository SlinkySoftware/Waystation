"""``python -m waystation_server``: run the server (uvicorn, uvloop, httptools) on port 8000.

Sets umask 0027 so blob files and directories are group-readable by the web tier only (ARCHITECTURE §4.5).
With ``WAYSTATION_SERVER_WORKERS > 1`` the Prometheus metrics of all processes are merged through
``PROMETHEUS_MULTIPROC_DIR`` (a fresh directory on the tmpfs, set before uvicorn starts the processes; ``metrics``).
Extra arguments are passed to uvicorn's options (``--reload`` and ``--reload-dir`` are used by the dev compose).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import uvicorn

from waystation_common.logs import configure_logging
from waystation_common.settings import get_settings
from waystation_server.metrics import MULTIPROC_ENV

PORT = 8000
MULTIPROC_DIR = Path('/tmp/waystation-metrics')  # noqa: S108 - the container's tmpfs, owned by this process


def prepare_multiprocess_metrics(workers: int, directory: Path = MULTIPROC_DIR) -> None:
    """Several processes: point ``PROMETHEUS_MULTIPROC_DIR`` at an empty directory (inherited by the workers)."""
    if workers <= 1:
        os.environ.pop(MULTIPROC_ENV, None)
        return
    shutil.rmtree(directory, ignore_errors=True)  # values of a previous run must not be merged in
    directory.mkdir(mode=0o700, parents=True)
    os.environ[MULTIPROC_ENV] = str(directory)


def main(argv: list[str]) -> None:  # pragma: no cover - process entry point, exercised by the container stack
    os.umask(0o027)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    reload_dirs = [argv[i + 1] for i, arg in enumerate(argv) if arg == '--reload-dir' and i + 1 < len(argv)]
    reload = '--reload' in argv
    prepare_multiprocess_metrics(1 if reload else settings.server_workers)
    uvicorn.run(
        'waystation_server.app:create_app',
        factory=True,
        host='0.0.0.0',  # noqa: S104 - container port, only reachable on the compose network
        port=PORT,
        loop='uvloop',
        http='httptools',
        workers=None if reload else settings.server_workers,
        reload=reload,
        reload_dirs=reload_dirs or None,
        log_config=None,
        server_header=False,
        date_header=True,
        timeout_graceful_shutdown=30,
    )


if __name__ == '__main__':  # pragma: no cover
    main(sys.argv[1:])
