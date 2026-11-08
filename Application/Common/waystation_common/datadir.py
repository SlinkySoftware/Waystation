"""Data-dir layout, ownership and modes (ARCHITECTURE §4.5, §5). Run by the ``init`` container.

Layout under ``WAYSTATION_DATA_DIR``::

    <root>/            2750 uid:gid   (web traverses via the shared group)
      blobs/           2750 dirs, 0640 files
      uploads/         2750 dirs, 0640 files
      keys/            0700 dirs, 0600 files (never readable by the web tier)

``prepare_data_dir`` is idempotent and only changes what is wrong. It always fixes the root and the three top-level
directories; it walks the trees (which can hold millions of files) only when ``recursive=True`` or when a top-level
directory had to be fixed (first start, or a restore with wrong ownership). Changing ownership needs root
(CAP_CHOWN); when not root, ownership is left alone and only modes the process owns are fixed.

Synchronous: call it from ``asyncio.to_thread`` in async code.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger('waystation.datadir')

DIR_MODE = 0o2750
FILE_MODE = 0o640
KEYS_DIR_MODE = 0o700
KEYS_FILE_MODE = 0o600
SUBDIRS = {'blobs': (DIR_MODE, FILE_MODE), 'uploads': (DIR_MODE, FILE_MODE), 'keys': (KEYS_DIR_MODE, KEYS_FILE_MODE)}


@dataclass
class PrepareReport:
    created: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    walked: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.created or self.fixed)


def _fix(path: Path, mode: int, uid: int, gid: int, *, can_chown: bool, report: PrepareReport) -> bool:
    """Make ``path`` (not a symlink) have ``mode`` and, when allowed, ``uid:gid``. True when something changed."""
    st = path.lstat()
    changed = False
    if can_chown and (st.st_uid != uid or st.st_gid != gid):
        os.lchown(path, uid, gid)
        changed = True
    if stat.S_IMODE(st.st_mode) != mode and (can_chown or st.st_uid == os.geteuid()):
        path.chmod(mode)
        changed = True
    if changed:
        report.fixed.append(str(path))
    return changed


def _walk(
    top: Path, dir_mode: int, file_mode: int, uid: int, gid: int, *, can_chown: bool, report: PrepareReport
) -> None:
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        base = Path(dirpath)
        for name in dirnames:
            child = base / name
            if child.is_symlink():
                log.warning('symlink in data dir ignored', extra={'path': str(child)})
                continue
            _fix(child, dir_mode, uid, gid, can_chown=can_chown, report=report)
        for name in filenames:
            child = base / name
            if child.is_symlink():
                log.warning('symlink in data dir ignored', extra={'path': str(child)})
                continue
            _fix(child, file_mode, uid, gid, can_chown=can_chown, report=report)


def prepare_data_dir(root: Path, uid: int, gid: int, *, recursive: bool = False) -> PrepareReport:
    report = PrepareReport()
    can_chown = os.geteuid() == 0
    if not root.exists():
        root.mkdir(parents=True)
        report.created.append(str(root))
    top_changed = _fix(root, DIR_MODE, uid, gid, can_chown=can_chown, report=report)
    for name, (dir_mode, _) in SUBDIRS.items():
        sub = root / name
        if not sub.exists():
            sub.mkdir()
            report.created.append(str(sub))
        top_changed |= _fix(sub, dir_mode, uid, gid, can_chown=can_chown, report=report)
    if recursive or top_changed:
        report.walked = True
        for name, (dir_mode, file_mode) in SUBDIRS.items():
            _walk(root / name, dir_mode, file_mode, uid, gid, can_chown=can_chown, report=report)
    return report


def drop_privileges(uid: int, gid: int) -> None:
    """When running as root, switch to ``uid:gid`` (supplementary groups: ``gid`` only). No-op otherwise."""
    if os.geteuid() != 0:
        return
    os.setgroups([gid])
    os.setgid(gid)
    os.setuid(uid)
