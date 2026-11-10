"""waystation_common.openpgp: armor, packets, keys, detached signature verification and the encoders."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from waystation_common import openpgp
from waystation_common.openpgp import OpenPgpError

FIXTURES = Path(__file__).parent / 'fixtures' / 'openpgp'
NOW = 1_800_000_000


def _rsa() -> tuple[rsa.RSAPrivateKey, openpgp.PublicKey]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = openpgp.parse_public_key(openpgp.rsa_key_body(private.public_key(), NOW - 100))
    assert key is not None
    return private, key


def _ed() -> tuple[ed25519.Ed25519PrivateKey, openpgp.PublicKey]:
    private = ed25519.Ed25519PrivateKey.generate()
    key = openpgp.parse_public_key(openpgp.ed25519_key_body(private.public_key(), NOW - 100))
    assert key is not None
    return private, key


RSA = _rsa()
ED = _ed()


# ---------------------------------------------------------------------------- real upstream signatures
@pytest.mark.parametrize(
    ('key', 'doc'), [('RPM-GPG-KEY-Rocky-10', 'rocky-10-extras'), ('RPM-GPG-KEY-AlmaLinux-10', 'alma-10-extras')]
)
def test_verifies_real_distribution_repomd_signatures(key: str, doc: str) -> None:
    keys = openpgp.read_public_keys((FIXTURES / key).read_text())
    data = (FIXTURES / f'{doc}-repomd.xml').read_bytes()
    signature = (FIXTURES / f'{doc}-repomd.xml.asc').read_bytes()
    signer = openpgp.verify_detached(data, signature, keys)
    assert signer in keys
    with pytest.raises(OpenPgpError, match='does not verify'):
        openpgp.verify_detached(data + b' ', signature, keys)


def test_a_distribution_signature_needs_its_own_key() -> None:
    other = openpgp.read_public_keys((FIXTURES / 'RPM-GPG-KEY-AlmaLinux-10').read_text())
    data = (FIXTURES / 'rocky-10-extras-repomd.xml').read_bytes()
    signature = (FIXTURES / 'rocky-10-extras-repomd.xml.asc').read_bytes()
    with pytest.raises(OpenPgpError, match='no configured key'):
        openpgp.verify_detached(data, signature, other)


# ---------------------------------------------------------------------------- round trips
@pytest.mark.parametrize('pair', [RSA, ED], ids=['rsa', 'ed25519'])
def test_sign_and_verify_round_trip(pair: tuple[openpgp.SigningKey, openpgp.PublicKey]) -> None:
    private, key = pair
    asc = openpgp.sign_detached(private, key, b'<repomd/>', created=NOW)
    assert asc.startswith('-----BEGIN PGP SIGNATURE-----\n')
    assert openpgp.verify_detached(b'<repomd/>', asc, [key], now=NOW) == key
    with pytest.raises(OpenPgpError):
        openpgp.verify_detached(b'<repomd />', asc, [key], now=NOW)


def test_certificate_reads_back_and_its_fingerprint_matches() -> None:
    private, key = RSA
    cert = openpgp.certificate(private, key, 'Waystation repository signing key (test)', created=NOW)
    keys = openpgp.read_public_keys(cert)
    assert [k.fingerprint for k in keys] == [key.fingerprint]
    assert len(key.fingerprint_hex) == 40
    assert key.key_id == key.fingerprint[-8:]
    found = openpgp.packets(openpgp.dearmor(cert)[0])
    assert [p.tag for p in found] == [openpgp.TAG_PUBLIC_KEY, openpgp.TAG_USER_ID, openpgp.TAG_SIGNATURE]
    sig = openpgp.parse_signature(found[2].body)
    assert sig is not None and sig.sig_type == openpgp.SIG_POSITIVE_CERT


def test_binary_signature_and_key_inputs() -> None:
    private, key = ED
    asc = openpgp.sign_detached(private, key, b'data', created=NOW)
    binary = openpgp.dearmor(asc)[0]
    assert openpgp.verify_detached(b'data', binary, [key], now=NOW) == key
    cert = openpgp.dearmor(openpgp.certificate(private, key, 'k', created=NOW))[0]
    assert openpgp.read_public_keys(cert)[0].fingerprint == key.fingerprint


def test_text_signature_canonicalises_line_endings() -> None:
    private, key = RSA
    packet = openpgp.make_signature(private, key, openpgp.SIG_TEXT, b'a\r\nb\r\n', created=NOW)
    assert openpgp.verify_detached(b'a\nb\n', packet, [key], now=NOW) == key
    assert openpgp.canonical_text(b'a\r\nb\nc') == b'a\r\nb\r\nc'


def test_picks_the_right_key_among_several_and_skips_other_algorithms() -> None:
    private, key = RSA
    asc = openpgp.sign_detached(private, key, b'x', created=NOW)
    assert openpgp.verify_detached(b'x', asc, [ED[1], _rsa()[1], key], now=NOW) == key


# ---------------------------------------------------------------------------- refusals
def _sig(private: openpgp.SigningKey, key: openpgp.PublicKey, **kw: object) -> bytes:
    return openpgp.make_signature(private, key, openpgp.SIG_BINARY, b'x', created=NOW, **kw)  # type: ignore[arg-type]


def test_refuses_weak_hashes_expired_and_unknown_critical() -> None:
    private, key = RSA
    with pytest.raises(KeyError):
        _sig(private, key, hash_algorithm=2)
    expired = _sig(private, key, hashed_extra=openpgp.subpacket(openpgp.SUB_SIG_EXPIRES, (10).to_bytes(4, 'big')))
    assert openpgp.verify_detached(b'x', expired, [key], now=NOW + 5) == key
    with pytest.raises(OpenPgpError, match='expired'):
        openpgp.verify_detached(b'x', expired, [key], now=NOW + 11)
    critical = _sig(private, key, hashed_extra=openpgp.subpacket(100, b'?', critical=True))
    with pytest.raises(OpenPgpError, match='critical'):
        openpgp.verify_detached(b'x', critical, [key], now=NOW)
    # A non-critical unknown subpacket is fine.
    assert openpgp.verify_detached(b'x', _sig(private, key, hashed_extra=openpgp.subpacket(100, b'?')), [key], now=NOW)


def _with_hash(packet: bytes, hash_algorithm: int) -> bytes:
    body = bytearray(openpgp.packets(packet)[0].body)
    body[3] = hash_algorithm
    return openpgp.encode_packet(openpgp.TAG_SIGNATURE, bytes(body))


def test_refuses_sha1_and_non_document_signatures() -> None:
    private, key = RSA
    with pytest.raises(OpenPgpError, match='weak hash'):
        openpgp.verify_detached(b'x', _with_hash(_sig(private, key), 2), [key], now=NOW)
    cert = openpgp.make_signature(private, key, openpgp.SIG_POSITIVE_CERT, b'x', created=NOW)
    with pytest.raises(OpenPgpError, match='not a document signature'):
        openpgp.verify_detached(b'x', cert, [key], now=NOW)


def test_wrong_issuer_and_bad_values_do_not_verify() -> None:
    _, key = RSA
    other_private, other = _rsa()
    sig = _sig(other_private, other)
    with pytest.raises(OpenPgpError, match='no configured key'):
        openpgp.verify_detached(b'x', sig, [key], now=NOW)
    # A signature value longer than the key, and a corrupted EdDSA value.
    assert not key.verify(b'\x01' * 1024, b'\x00' * 32, openpgp.HASHES[8][1])
    assert not ED[1].verify((b'\x01' * 32, b'\x02' * 32), b'\x00' * 32, openpgp.HASHES[8][1])


def test_unsupported_packets_are_skipped() -> None:
    assert openpgp.parse_public_key(bytes([3]) + b'\x00' * 10) is None
    assert openpgp.parse_public_key(bytes([4]) + b'\x00' * 4 + bytes([19]) + b'\x00' * 4) is None
    bad_oid = bytes([4]) + b'\x00' * 4 + bytes([22, 3]) + b'\x2b\x06\x01' + openpgp.mpi(b'\x40' + b'\x01' * 32)
    assert openpgp.parse_public_key(bad_oid) is None
    v6 = bytes([4]) + b'\x00' * 4 + bytes([27]) + b'\x07' * 32
    assert openpgp.parse_public_key(v6) is not None
    assert openpgp.parse_signature(bytes([3]) + b'\x00' * 10) is None
    unknown = bytes([4, 0, 19, 8]) + b'\x00\x00' + b'\x00\x00' + b'\x00\x00'
    assert openpgp.parse_signature(unknown) is None
    with pytest.raises(OpenPgpError, match='no supported OpenPGP signature'):
        openpgp.read_signatures(openpgp.encode_packet(openpgp.TAG_SIGNATURE, unknown))
    with pytest.raises(OpenPgpError, match='no supported OpenPGP public key'):
        openpgp.read_public_keys(openpgp.encode_packet(openpgp.TAG_USER_ID, b'only a user id'))


def test_ed25519_v4_native_signature() -> None:
    private = ed25519.Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes_raw()
    key = openpgp.parse_public_key(bytes([4]) + NOW.to_bytes(4, 'big') + bytes([27]) + raw)
    assert key is not None
    hashed_area = openpgp.subpacket(openpgp.SUB_CREATED, NOW.to_bytes(4, 'big'))
    hashed = bytes([4, 0, 27, 10]) + len(hashed_area).to_bytes(2, 'big') + hashed_area
    import hashlib

    digest = hashlib.sha512(b'doc' + openpgp.trailer(hashed)).digest()
    body = hashed + b'\x00\x00' + digest[:2] + private.sign(digest)
    assert openpgp.verify_detached(b'doc', openpgp.encode_packet(2, body), [key], now=NOW) == key


# ---------------------------------------------------------------------------- armor and packet framing
def test_armor_headers_checksum_and_several_blocks() -> None:
    _, key = RSA
    cert = openpgp.certificate(RSA[0], key, 'a', created=NOW)
    with_headers = cert.replace('\n\n', '\nVersion: test\nComment: two blocks\n\n', 1)
    two = 'leading text\n' + with_headers + '\n' + openpgp.certificate(ED[0], ED[1], 'b', created=NOW)
    assert [k.fingerprint for k in openpgp.read_public_keys(two)] == [key.fingerprint, ED[1].fingerprint]
    no_blank = cert.replace('\n\n', '\n', 1)
    assert openpgp.read_public_keys(no_blank)[0].fingerprint == key.fingerprint
    lines = cert.splitlines()
    no_crc = '\n'.join(line for line in lines if not (line.startswith('=') and len(line) == 5))
    assert openpgp.read_public_keys(no_crc)[0].fingerprint == key.fingerprint
    bad_crc = '\n'.join('=AAAA' if line.startswith('=') and len(line) == 5 else line for line in lines)
    with pytest.raises(OpenPgpError, match='checksum'):
        openpgp.read_public_keys(bad_crc)
    with pytest.raises(OpenPgpError, match='not valid base64'):
        openpgp.dearmor('-----BEGIN PGP SIGNATURE-----\n\n!!!!\n-----END PGP SIGNATURE-----\n')
    with pytest.raises(OpenPgpError, match='not terminated'):
        openpgp.dearmor('-----BEGIN PGP SIGNATURE-----\n\nAAAA\n')
    with pytest.raises(OpenPgpError, match='no armored'):
        openpgp.dearmor('-----BEGIN NOTHING-----\n')
    with pytest.raises(OpenPgpError, match='no armored'):
        openpgp.dearmor(cert, 'SIGNATURE')
    with pytest.raises(OpenPgpError, match='no OpenPGP data'):
        openpgp.dearmor(b'')
    with pytest.raises(OpenPgpError, match='too large'):
        openpgp.dearmor(b'x' * (openpgp.MAX_INPUT + 1))


def test_packet_framing() -> None:
    for size in (0, 191, 192, 8383, 8384, 70000):
        body = b'\x01' * size
        assert openpgp.packets(openpgp.encode_packet(13, body)) == [openpgp.Packet(13, body)]
    # Old-format headers with one-, two- and four-octet lengths.
    assert openpgp.packets(bytes([0x80 | (13 << 2), 2]) + b'ab') == [openpgp.Packet(13, b'ab')]
    assert openpgp.packets(bytes([0x80 | (13 << 2) | 1, 0, 2]) + b'ab') == [openpgp.Packet(13, b'ab')]
    assert openpgp.packets(bytes([0x80 | (13 << 2) | 2, 0, 0, 0, 2]) + b'ab') == [openpgp.Packet(13, b'ab')]
    with pytest.raises(OpenPgpError, match='indeterminate'):
        openpgp.packets(bytes([0x80 | (13 << 2) | 3]) + b'ab')
    with pytest.raises(OpenPgpError, match='partial'):
        openpgp.packets(bytes([0xC0 | 13, 224]) + b'ab')
    with pytest.raises(OpenPgpError, match='not an OpenPGP packet'):
        openpgp.packets(b'\x00')
    with pytest.raises(OpenPgpError, match='truncated'):
        openpgp.packets(bytes([0xC0 | 13, 5]) + b'ab')
    with pytest.raises(OpenPgpError, match='too many'):
        openpgp.packets(bytes([0xC0 | 13, 0]) * (openpgp.MAX_PACKETS + 1))


def test_subpacket_lengths_and_empty_subpackets() -> None:
    for size in (10, 300, 9000):
        encoded = openpgp.subpacket(20, b'n' * size)
        assert list(openpgp._subpackets(encoded)) == [(20, False, b'n' * size)]
    with pytest.raises(OpenPgpError, match='empty'):
        list(openpgp._subpackets(b'\x00'))


def test_mpi_and_crc() -> None:
    assert openpgp.mpi(0) == b'\x00\x00'
    assert openpgp.mpi(1) == b'\x00\x01\x01'
    assert openpgp.mpi(b'\x00\x80') == b'\x00\x08\x80'
    assert base64.b64encode(openpgp.crc24(b'').to_bytes(3, 'big')) == b'twTO'
