import os
import stat
from pathlib import Path

import pytest

from waystation_common import datadir
from waystation_common.datadir import DIR_MODE, FILE_MODE, KEYS_DIR_MODE, drop_privileges, prepare_data_dir


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_creates_layout_and_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / 'data'
    uid, gid = os.geteuid(), os.getegid()
    first = prepare_data_dir(root, uid, gid)
    assert first.changed
    assert {Path(p).name for p in first.created} == {'data', 'blobs', 'uploads', 'keys'}
    assert mode(root) == DIR_MODE
    assert mode(root / 'blobs') == DIR_MODE
    assert mode(root / 'keys') == KEYS_DIR_MODE
    second = prepare_data_dir(root, uid, gid)
    assert not second.changed
    assert not second.walked


def test_walks_and_fixes_when_top_level_wrong(tmp_path: Path) -> None:
    root = tmp_path / 'data'
    uid, gid = os.geteuid(), os.getegid()
    prepare_data_dir(root, uid, gid)
    nested = root / 'blobs' / 'sha256' / 'aa'
    nested.mkdir(parents=True)
    nested.chmod(0o777)
    blob = nested / 'x'
    blob.write_bytes(b'1')
    blob.chmod(0o666)
    key = root / 'keys' / 'k.enc'
    key.write_bytes(b'k')
    key.chmod(0o644)
    (root / 'blobs' / 'link').symlink_to(blob)
    (root / 'blobs' / 'dirlink').symlink_to(nested, target_is_directory=True)

    # top level correct: no walk, nested stays wrong
    assert not prepare_data_dir(root, uid, gid).walked
    assert mode(blob) == 0o666

    (root / 'uploads').chmod(0o755)  # a wrong top-level dir triggers the walk
    report = prepare_data_dir(root, uid, gid)
    assert report.walked
    assert mode(nested) == DIR_MODE
    assert mode(blob) == FILE_MODE
    assert mode(key) == 0o600
    assert (root / 'blobs' / 'link').is_symlink()

    blob.chmod(0o604)
    assert prepare_data_dir(root, uid, gid, recursive=True).fixed == [str(blob)]


def test_drop_privileges_noop_when_not_root(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(datadir.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(datadir.os, 'setuid', lambda uid: calls.append('setuid'))
    drop_privileges(10001, 10001)
    assert calls == []


def test_drop_privileges_as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(datadir.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(datadir.os, 'setgroups', lambda groups: calls.append(('setgroups', groups)))
    monkeypatch.setattr(datadir.os, 'setgid', lambda gid: calls.append(('setgid', gid)))
    monkeypatch.setattr(datadir.os, 'setuid', lambda uid: calls.append(('setuid', uid)))
    drop_privileges(10001, 10002)
    assert calls == [('setgroups', [10002]), ('setgid', 10002), ('setuid', 10001)]


def test_chown_branch_when_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    chowned: list[tuple[str, int, int]] = []
    monkeypatch.setattr(datadir.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(datadir.os, 'lchown', lambda p, u, g: chowned.append((str(p), u, g)))
    prepare_data_dir(tmp_path / 'data', 4242, 4243)
    assert (str(tmp_path / 'data'), 4242, 4243) in chowned
