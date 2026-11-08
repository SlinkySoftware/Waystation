"""``python -m waystation_server``: run the server (uvicorn, uvloop, httptools) on port 8000.

Sets umask 0027 so blob files and directories are group-readable by the web tier only (ARCHITECTURE §4.5).
Extra arguments are passed to uvicorn's options (``--reload`` and ``--reload-dir`` are used by the dev compose).
"""

from __future__ import annotations

import os
import sys

import uvicorn

from waystation_common.logs import configure_logging
from waystation_common.settings import get_settings

PORT = 8000


def main(argv: list[str]) -> None:  # pragma: no cover - process entry point, exercised by the container stack
    os.umask(0o027)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    reload_dirs = [argv[i + 1] for i, arg in enumerate(argv) if arg == '--reload-dir' and i + 1 < len(argv)]
    reload = '--reload' in argv
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
