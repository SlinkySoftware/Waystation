"""Worker liveness without an HTTP listener: the main loop touches a heartbeat file; the container healthcheck
(``python -m waystation_worker.health``) fails when it is older than ``MAX_AGE_S``."""

from __future__ import annotations

import sys
import time
from pathlib import Path

HEARTBEAT_FILE = Path('/tmp/waystation-worker.heartbeat')  # noqa: S108 - tmpfs inside the container
INTERVAL_S = 10.0
MAX_AGE_S = 45.0


def beat(path: Path = HEARTBEAT_FILE) -> None:
    """Record liveness (tiny tmpfs write; called via ``asyncio.to_thread``)."""
    path.write_text(f'{time.time():.0f}\n')


def is_healthy(path: Path = HEARTBEAT_FILE, max_age_s: float = MAX_AGE_S) -> bool:
    try:
        return time.time() - path.stat().st_mtime <= max_age_s
    except FileNotFoundError:
        return False


if __name__ == '__main__':  # pragma: no cover
    sys.exit(0 if is_healthy() else 1)
