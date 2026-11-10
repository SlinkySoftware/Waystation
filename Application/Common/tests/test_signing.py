"""waystation_common.signing: per-repository keys (created once, encrypted at rest, bound to their repository) and
detached signatures that waystation_common.openpgp verifies."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from waystation_common import openpgp
from waystation_common.crypto import Keyring, generate_key
from waystation_common.errors import CryptoError
from waystation_common.signing import KeyStore

RING = Keyring.from_strings([generate_key()])


@pytest.fixture(scope='module')
def store(tmp_path_factory: pytest.TempPathFactory) -> KeyStore:
    return KeyStore.in_data_dir(tmp_path_factory.mktemp('data'), RING)


def test_creates_once_then_loads_the_same_key(store: KeyStore) -> None:
    first = store.load_or_create('repo-1', 'Waystation repository mine (repo-1)')
    again = store.load_or_create('repo-1', 'ignored once created')
    assert again.fingerprint == first.fingerprint
    assert store.exists('repo-1')
    path = store.path('repo-1')
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    doc = json.loads(path.read_text())
    assert doc['algorithm'] == 'rsa3072' and doc['fingerprint'] == first.fingerprint
    assert doc['private'].startswith('ws1.') and 'PRIVATE' not in path.read_text()
    assert not list(store.root.glob('.*.tmp'))


def test_signatures_verify_with_the_served_certificate(store: KeyStore) -> None:
    key = store.load_or_create('repo-2', 'Waystation repository other (repo-2)')
    asc = key.sign(b'<repomd/>')
    keys = openpgp.read_public_keys(key.certificate)
    assert openpgp.verify_detached(b'<repomd/>', asc, keys).fingerprint_hex == key.fingerprint
    with pytest.raises(openpgp.OpenPgpError):
        openpgp.verify_detached(b'<repomd />', asc, keys)
    other = store.load_or_create('repo-3', 'third')
    with pytest.raises(openpgp.OpenPgpError):
        openpgp.verify_detached(b'<repomd/>', asc, openpgp.read_public_keys(other.certificate))


def test_a_key_file_is_bound_to_its_repository_and_secret(store: KeyStore, tmp_path: Path) -> None:
    store.load_or_create('repo-4', 'four')
    moved = KeyStore(tmp_path, RING)
    (tmp_path / 'repo-5.json').write_text(store.path('repo-4').read_text())
    with pytest.raises(CryptoError):
        moved.load('repo-5')  # associated data names repo-4
    (tmp_path / 'repo-4.json').write_text(store.path('repo-4').read_text())
    with pytest.raises(CryptoError):
        KeyStore(tmp_path, Keyring.from_strings([generate_key()])).load('repo-4')
    doc = json.loads(store.path('repo-4').read_text())
    (tmp_path / 'repo-4.json').write_text(json.dumps({**doc, 'fingerprint': '0' * 40}))
    with pytest.raises(CryptoError, match='fingerprint'):
        moved.load('repo-4')
    (tmp_path / 'repo-4.json').write_text(json.dumps({**doc, 'format': 9}))
    with pytest.raises(CryptoError, match='unsupported'):
        moved.load('repo-4')


def test_a_concurrent_creator_wins_and_both_agree(store: KeyStore, monkeypatch: pytest.MonkeyPatch) -> None:
    winner = store.load_or_create('repo-6', 'six')
    # Simulate the race: the file appears between the existence check and the link.
    real_exists = Path.exists
    calls = {'n': 0}

    def exists(self: Path) -> bool:
        if self == store.path('repo-6') and calls['n'] == 0:
            calls['n'] += 1
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, 'exists', exists)
    loser = store.load_or_create('repo-6', 'six')
    assert loser.fingerprint == winner.fingerprint


def test_refuses_unsafe_repository_ids(store: KeyStore) -> None:
    for bad in ('../x', 'a/b', '', 'x' * 65):
        with pytest.raises(CryptoError):
            store.path(bad)
