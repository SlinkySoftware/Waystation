"""waystation_common.signing: per-repository keys (created once, encrypted at rest, bound to their repository), the
packets it writes (read back by waystation_common.openpgp), and the real distribution signatures the yum proxy
verifies with the same reader."""

from __future__ import annotations

import base64
import json
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from waystation_common import openpgp, signing
from waystation_common.crypto import Keyring, generate_key
from waystation_common.errors import CryptoError
from waystation_common.signing import KeyStore

RING = Keyring.from_strings([generate_key()])
FIXTURES = Path(__file__).parent / 'fixtures' / 'openpgp'
NOW = 1_800_000_000


@pytest.fixture(scope='module')
def store(tmp_path_factory: pytest.TempPathFactory) -> KeyStore:
    return KeyStore.in_data_dir(tmp_path_factory.mktemp('data'), RING)


# ---------------------------------------------------------------------------- real distribution signatures
@pytest.mark.parametrize(
    ('key', 'doc'), [('RPM-GPG-KEY-Rocky-10', 'rocky-10-extras'), ('RPM-GPG-KEY-AlmaLinux-10', 'alma-10-extras')]
)
def test_real_repomd_signatures_verify_with_the_distribution_keys(key: str, doc: str) -> None:
    certs = openpgp.read_certs((FIXTURES / key).read_bytes())
    data = (FIXTURES / f'{doc}-repomd.xml').read_bytes()
    signature = (FIXTURES / f'{doc}-repomd.xml.asc').read_bytes()
    assert openpgp.verify_detached(data, signature, certs).cert_fingerprint == certs[0].fingerprint
    with pytest.raises(openpgp.OpenPgpError):
        openpgp.verify_detached(data + b' ', signature, certs)
    other = 'RPM-GPG-KEY-AlmaLinux-10' if 'Rocky' in key else 'RPM-GPG-KEY-Rocky-10'
    with pytest.raises(openpgp.OpenPgpError):
        openpgp.verify_detached(data, signature, openpgp.read_certs((FIXTURES / other).read_bytes()))


# ---------------------------------------------------------------------------- packets
def test_certificate_and_signature_read_back() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = signing.PublicPart.of(private, NOW - 10)
    cert = signing.certificate(private, key, 'Waystation repository test (r1)', created=NOW - 10)
    (parsed,) = openpgp.read_certs(cert.encode())
    assert parsed.fingerprint == key.fingerprint_hex and parsed.primary.can_sign(NOW)
    asc = signing.sign_detached(private, key, b'<repomd/>', created=NOW)
    verified = openpgp.verify_detached(b'<repomd/>', asc.encode(), [parsed], now=NOW)
    assert verified.key_fingerprint == key.fingerprint_hex and verified.created == NOW
    with pytest.raises(openpgp.OpenPgpError):
        openpgp.verify_detached(b'<repomd />', asc.encode(), [parsed], now=NOW)
    # The armor's CRC-24 is checked by the reader.
    lines = asc.splitlines()
    bad = '\n'.join('=AAAA' if line.startswith('=') and len(line) == 5 else line for line in lines)
    with pytest.raises(openpgp.OpenPgpError):
        openpgp.verify_detached(b'<repomd/>', bad.encode(), [parsed], now=NOW)
    assert signing.sign_detached(private, key, b'x').startswith('-----BEGIN PGP SIGNATURE-----\n')
    assert signing.certificate(private, key, 'now').startswith('-----BEGIN PGP PUBLIC KEY BLOCK-----\n')


def test_packet_and_subpacket_lengths() -> None:
    for size in (0, 191, 192, 8383, 8384, 70000):
        body = b'\x01' * size
        assert list(openpgp.packets(signing.packet(13, body))) == [(13, body)]
    assert signing.subpacket(20, b'n' * 300)[:2] == bytes([((301 - 192) >> 8) + 192, (301 - 192) & 0xFF])
    assert signing.subpacket(20, b'n' * 9000)[:1] == b'\xff'
    assert signing.subpacket(2, b'x', critical=True)[1] == 0x82
    assert signing.mpi(0) == b'\x00\x00'
    assert signing.mpi(1) == b'\x00\x01\x01'
    assert signing.mpi(b'\x00\x80') == b'\x00\x08\x80'
    assert base64.b64encode(signing._crc24(b'').to_bytes(3, 'big')) == b'twTO'


# ---------------------------------------------------------------------------- the key store
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
    asc = key.sign(b'<repomd/>').encode()
    certs = openpgp.read_certs(key.certificate.encode())
    assert openpgp.verify_detached(b'<repomd/>', asc, certs).cert_fingerprint == key.fingerprint
    other = store.load_or_create('repo-3', 'third')
    with pytest.raises(openpgp.OpenPgpError):
        openpgp.verify_detached(b'<repomd/>', asc, openpgp.read_certs(other.certificate.encode()))


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
    # The race: the file appears between the existence check and the link.
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
