"""Re-encrypting the files in ``keys/`` after a secret-key rotation (T-O11, ``waystation_common.keyfiles``)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from waystation_common import keyfiles
from waystation_common.crypto import Keyring, generate_key, kid_prefix
from waystation_common.errors import CryptoError
from waystation_common.signing import KeyStore

OLD, NEW, OTHER = generate_key(), generate_key(), generate_key()
OLD_RING = Keyring.from_strings([OLD])
ROTATED = Keyring.from_strings([NEW, OLD])
NEW_ONLY = Keyring.from_strings([NEW])


@pytest.fixture(scope='module')
def signing_key(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A repository signing key file made under the old key (generated once: RSA 3072 is slow)."""
    root = tmp_path_factory.mktemp('keys')
    KeyStore(root, OLD_RING).load_or_create('repo1', 'Waystation repository test (repo1)')
    return (root / 'repo1.json').read_text()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_signing_key_file_is_rewritten_and_still_loads(tmp_path: Path, signing_key: str) -> None:
    path = write(tmp_path / 'keys' / 'repo1.json', signing_key)
    before = KeyStore(tmp_path / 'keys', OLD_RING).load('repo1')
    assert keyfiles.needs_rotation(path, ROTATED)
    assert keyfiles.rotate_file(path, ROTATED) is True
    assert not keyfiles.needs_rotation(path, ROTATED)
    doc, old_doc = json.loads(path.read_text()), json.loads(signing_key)
    assert doc['private'].startswith(kid_prefix(ROTATED.active_kid))
    assert {k: v for k, v in doc.items() if k != 'private'} == {k: v for k, v in old_doc.items() if k != 'private'}
    after = KeyStore(tmp_path / 'keys', NEW_ONLY).load('repo1')  # the old key is no longer needed
    assert after.public.fingerprint_hex == before.public.fingerprint_hex
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert keyfiles.rotate_file(path, ROTATED) is False  # second run: nothing to do
    assert sorted(p.name for p in path.parent.iterdir()) == ['repo1.json']  # no temporary file left


def test_registry_token_key_is_rewritten(tmp_path: Path) -> None:
    raw = os.urandom(32)
    path = write(
        tmp_path / keyfiles.REGISTRY_TOKEN_KEY_FILE, OLD_RING.encrypt(raw, aad=keyfiles.REGISTRY_TOKEN_KEY_AAD)
    )
    path.chmod(0o640)
    assert keyfiles.rotate_file(path, ROTATED) is True
    text = path.read_text()
    assert text.endswith('\n') and text.count('\n') == 1
    assert NEW_ONLY.decrypt(text.strip(), aad=keyfiles.REGISTRY_TOKEN_KEY_AAD) == raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_undecryptable_and_malformed_files_are_left_alone(tmp_path: Path, signing_key: str) -> None:
    foreign = Keyring.from_strings([OTHER])
    path = write(
        tmp_path / keyfiles.REGISTRY_TOKEN_KEY_FILE, foreign.encrypt(b'x', aad=keyfiles.REGISTRY_TOKEN_KEY_AAD)
    )
    before = path.read_text()
    with pytest.raises(CryptoError):
        keyfiles.rotate_file(path, ROTATED)
    assert path.read_text() == before
    renamed = write(tmp_path / 'repo2.json', signing_key)  # bound to repo1: does not decrypt as repo2
    with pytest.raises(CryptoError):
        keyfiles.rotate_file(renamed, ROTATED)
    for name, text in (('bad.json', '{not json'), ('empty.json', '{}'), ('list.json', '[1]')):
        with pytest.raises(CryptoError):
            keyfiles.rotate_file(write(tmp_path / name, text), ROTATED)


def test_key_files_lists_only_key_files(tmp_path: Path) -> None:
    assert keyfiles.key_files(tmp_path / 'missing') == []
    for name in ('b.json', 'a.json', keyfiles.REGISTRY_TOKEN_KEY_FILE, '.a.json.x.tmp', 'notes.txt'):
        write(tmp_path / name, '')
    (tmp_path / 'dir.json').mkdir()
    assert [p.name for p in keyfiles.key_files(tmp_path)] == ['a.json', 'b.json', keyfiles.REGISTRY_TOKEN_KEY_FILE]
