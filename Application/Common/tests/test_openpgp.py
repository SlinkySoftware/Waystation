"""OpenPGP detached-signature verification (``waystation_common.openpgp``): generated keys of every supported
algorithm, the policy checks, malformed input, and the real HashiCorp and OpenTofu signatures recorded for the
Terraform client harness."""

import json
from pathlib import Path

import pytest
from ws_openpgp import NOW, PgpKey, Sub, armor, cert, detached, packet, signature, subpacket

from waystation_common import openpgp
from waystation_common.openpgp import OpenPgpError, dearmor, packets, read_certs, verify_detached

DATA = b'0123abcd  terraform-provider-demo_1.0.0_linux_amd64.zip\n'
FIXTURES = Path(__file__).resolve().parents[3] / 'tests' / 'fixtures' / 'upstreams' / 'terraform'


@pytest.fixture(scope='module')
def rsa_key() -> PgpKey:
    return PgpKey('rsa')


@pytest.mark.parametrize('algorithm', ['ed25519', 'ed25519v6', 'ed448', 'p256'])
def test_primary_key_signatures_verify(algorithm: str) -> None:
    key = PgpKey(algorithm)
    certs = read_certs(armor(cert(key)))
    assert len(certs) == 1
    assert certs[0].fingerprint == key.fingerprint.hex().upper()
    found = verify_detached(DATA, armor(detached(key, DATA), 'SIGNATURE'), certs)
    assert found.key_fingerprint == key.fingerprint.hex().upper()
    assert found.created > 0
    with pytest.raises(OpenPgpError, match='does not verify'):
        verify_detached(DATA + b'x', detached(key, DATA), certs)


def test_rsa_subkey_signature_and_hashes(rsa_key: PgpKey) -> None:
    primary = PgpKey('ed25519')
    certs = read_certs(cert(primary, subkeys=(Sub(rsa_key),)))
    for hash_id in (8, 9, 10, 11, 12, 14):
        found = verify_detached(DATA, detached(rsa_key, DATA, hash_id=hash_id), certs)
        assert found.cert_fingerprint == primary.fingerprint.hex().upper()
        assert found.key_fingerprint == rsa_key.fingerprint.hex().upper()
    with pytest.raises(OpenPgpError):  # SHA-1 is refused for data signatures
        verify_detached(DATA, detached(rsa_key, DATA, hash_id=2), certs)


def test_text_signature_canonicalises_line_endings() -> None:
    key = PgpKey()
    certs = read_certs(cert(key))
    sig = detached(key, DATA, sig_type=0x01)
    assert verify_detached(DATA.replace(b'\n', b'\r\n'), sig, certs)
    assert verify_detached(DATA, sig, certs)


def test_policy_refusals(rsa_key: PgpKey) -> None:
    key = PgpKey()
    good = read_certs(cert(key))
    cases = {
        'expired signature': detached(key, DATA, created=NOW - 100, hashed=[subpacket(3, (10).to_bytes(4, 'big'))]),
        'unknown critical': detached(key, DATA, hashed=[subpacket(99, b'x', critical=True)]),
        'no creation time': detached(key, DATA, created=-1),
        'other issuer': detached(key, DATA, issuer=False, unhashed=[subpacket(16, b'\x01' * 8)]),
        'unknown hash': packet(2, signature(key, DATA, 0, hash_id=8)[:3] + b'\x63' + signature(key, DATA, 0)[4:]),
    }
    for name, sig in cases.items():
        with pytest.raises(OpenPgpError):
            verify_detached(DATA, sig, good)
        assert name
    # a non-critical unknown subpacket and a signature without issuer subpackets are fine
    assert verify_detached(DATA, detached(key, DATA, hashed=[subpacket(99, b'x')]), good)
    assert verify_detached(DATA, detached(key, DATA, issuer=False), good)
    # not a data signature, or not version 4
    with pytest.raises(OpenPgpError, match='no version 4 data signature'):
        verify_detached(DATA, packet(2, signature(key, DATA, 0x13)), good)
    with pytest.raises(OpenPgpError, match='no version 4 data signature'):
        verify_detached(DATA, packet(2, signature(key, DATA, 0x00, version=3)), good)
    with pytest.raises(OpenPgpError, match='no version 4 data signature'):
        verify_detached(DATA, packet(13, b'not a signature'), good)
    # a key that is too small, or of an unsupported algorithm, signs nothing
    small = PgpKey('rsa', bits=1024)
    with pytest.raises(OpenPgpError):
        verify_detached(DATA, detached(small, DATA), read_certs(cert(small)))
    dsa = PgpKey('dsa')
    assert read_certs(cert(dsa, self_signed=False))[0].primary.public is None
    with pytest.raises(OpenPgpError):
        verify_detached(DATA, detached(dsa, DATA), read_certs(cert(dsa, self_signed=False)))
    # a signature by another key
    with pytest.raises(OpenPgpError):
        verify_detached(DATA, detached(rsa_key, DATA), good)


def test_key_validity() -> None:
    key = PgpKey()
    sig = detached(key, DATA)
    refused = {
        'no self-signature': cert(key, self_signed=False),
        'revoked': cert(key, revoked=True),
        'expired': cert(key, expires_after=10),
        'not for signing': cert(key, flags=0x01),
    }
    for name, data in refused.items():
        with pytest.raises(OpenPgpError):
            verify_detached(DATA, sig, read_certs(data))
        assert name
    assert verify_detached(DATA, sig, read_certs(cert(key, flags=None)))  # no key flags: any use
    assert verify_detached(DATA, sig, read_certs(cert(key, expires_after=10 * 365 * 86_400)))
    direct_only = cert(key, direct=True, self_signed=False)
    assert verify_detached(DATA, sig, read_certs(direct_only))
    # SHA-1 self-signatures (old keys) are accepted
    assert verify_detached(DATA, sig, read_certs(cert(key, hash_id=2)))
    assert verify_detached(DATA, sig, read_certs(cert(key, uid=b'\x00' * 2)))


def test_subkey_validity() -> None:
    primary, sub = PgpKey(), PgpKey()
    sig = detached(sub, DATA)
    refused = {
        'no binding': Sub(sub, binding=False),
        'bad binding': Sub(sub, bad_binding=True),
        'revoked': Sub(sub, revoked=True),
        'encryption only': Sub(sub, flags=0x0C),
        'expired': Sub(sub, expires_after=10),
    }
    for name, item in refused.items():
        with pytest.raises(OpenPgpError):
            verify_detached(DATA, sig, read_certs(cert(primary, subkeys=(item,))))
        assert name
    with pytest.raises(OpenPgpError):  # the primary key is revoked: so are its subkeys
        verify_detached(DATA, sig, read_certs(cert(primary, revoked=True, subkeys=(Sub(sub),))))
    assert verify_detached(DATA, sig, read_certs(cert(primary, subkeys=(Sub(sub, hash_id=2),))))
    certs = read_certs(cert(primary, subkeys=(Sub(PgpKey('dsa')), Sub(sub))))
    assert verify_detached(DATA, sig, certs).key_fingerprint == sub.fingerprint.hex().upper()


def test_newest_self_signature_wins() -> None:
    key = PgpKey()
    uid = b'Test <t@example.com>'
    body = packet(6, key.body) + packet(13, uid)
    signed = key.prefix() + b'\xb4' + len(uid).to_bytes(4, 'big') + uid
    newer = signature(key, signed, 0x13, created=NOW - 10, hashed=[subpacket(27, b'\x01')])
    older = signature(key, signed, 0x13, created=NOW - 1000, hashed=[subpacket(27, b'\x02')])
    with pytest.raises(OpenPgpError):  # the newer one says "certify only"
        verify_detached(DATA, detached(key, DATA), read_certs(body + packet(2, newer) + packet(2, older)))
    # signatures by other keys are skipped; a user attribute is certified like a user ID
    other = PgpKey()
    attribute = b'\x01photo'
    attr_signed = key.prefix() + b'\xd1' + len(attribute).to_bytes(4, 'big') + attribute
    extra = packet(2, signature(other, signed, 0x13)) + packet(2, older)
    extra += packet(17, attribute) + packet(2, signature(key, attr_signed, 0x13))
    assert verify_detached(DATA, detached(key, DATA), read_certs(body + extra))


def test_several_keys_and_old_format_packets() -> None:
    a, b = PgpKey(), PgpKey()
    block = armor(cert(a)) + b'\n' + armor(cert(b))
    certs = read_certs(block)
    assert [c.fingerprint for c in certs] == [a.fingerprint.hex().upper(), b.fingerprint.hex().upper()]
    assert verify_detached(DATA, detached(b, DATA), certs).cert_fingerprint == b.fingerprint.hex().upper()
    old = packet(2, signature(a, DATA, 0), old=True)
    assert verify_detached(DATA, old, certs)
    assert list(packets(bytes([0x88 | 3]) + b'xyz')) == [(2, b'xyz')]  # old format, indeterminate length
    assert list(packets(bytes([0x88]) + b'\x02ab')) == [(2, b'ab')]
    assert list(packets(bytes([0x89]) + b'\x00\x02ab')) == [(2, b'ab')]
    long_body = b'z' * 300
    assert list(packets(packet(13, long_body))) == [(13, long_body)]
    huge = b'z' * 9000
    assert list(packets(packet(13, huge))) == [(13, huge)]
    assert read_certs(packet(13, b'orphan user id')) == []
    v5 = packet(6, b'\x05' + a.body[1:])
    assert read_certs(v5 + packet(13, b'uid'))[0:0] == []


def test_malformed_input() -> None:
    key = PgpKey()
    good = cert(key)
    bad = [
        b'\x00\x01',  # not a packet
        b'\xc2',  # truncated length
        b'\xc2\xc5',  # truncated two-octet length
        b'\xc2\x05ab',  # body shorter than its length
        b'\xc2\xe1ab',  # partial length
        packet(5, b'secret'),  # secret key
        packet(6, b'\x04\x00'),  # truncated key
        packet(6, key.body[:-3]),
        b'\xc2' * 10_001,
    ]
    for data in bad:
        with pytest.raises(OpenPgpError):
            read_certs(data)
    with pytest.raises(OpenPgpError):
        read_certs(b'x' * (openpgp.MAX_INPUT + 1))
    many = packet(13, b'u') * (openpgp.MAX_PACKETS + 1)
    with pytest.raises(OpenPgpError, match='too many'):
        list(packets(many))
    armored = armor(good).decode()
    broken = {
        'no end': armored.replace('-----END PGP PUBLIC KEY BLOCK-----', ''),
        'not base64': armored.replace('Comment: test\n\n', 'Comment: test\n\n!!!!\n'),
        'bad crc': armored[: armored.index('\n=') + 2] + 'AAAA' + armored[armored.index('\n=') + 6 :],
        'none': '-----BEGIN PGP SOMETHING\nxx\n',
        'unicode': '-----BEGIN PGP PUBLIC KEY BLOCK-----\né\n',
    }
    for name, text in broken.items():
        with pytest.raises(OpenPgpError):
            dearmor(text.encode())
        assert name
    assert dearmor(armor(good, checksum=False)) == good
    assert dearmor(good) == good
    # invalid key material
    for body in (
        b'\x04\x00\x00\x00\x01\x13\x08' + bytes.fromhex('2a8648ce3d030107') + b'\x00\x08\x04',  # bad P-256 point
        b'\x04\x00\x00\x00\x01\x16\x09' + bytes.fromhex('2b06010401da470f01') + b'\x00\x08\x40',  # short EdDSA
        b'\x04\x00\x00\x00\x01\x13\x00',  # empty OID
        b'\x04\x00\x00\x00\x01\x01' + b'\x08\x00' + b'\xff' * 256 + b'\x00\x01\x00',  # RSA e = 0
    ):
        with pytest.raises(OpenPgpError):
            read_certs(packet(6, body))
    unknown_curve = b'\x04\x00\x00\x00\x01\x13\x03\x2b\x65\x70\x00\x08\x04'
    assert read_certs(packet(6, unknown_curve))[0].primary.public is None
    other_eddsa = b'\x04\x00\x00\x00\x01\x16\x03\x2b\x65\x71\x00\x08\x40'
    assert read_certs(packet(6, other_eddsa))[0].primary.public is None
    # malformed signatures
    sig = signature(key, DATA, 0)
    for body in (sig[:5], b'\x04\x00\x16\x08\x00\x01\x00', b'\x04\x00\x16\x08\x00\x02\x00\x00\x00\x00'):
        with pytest.raises(OpenPgpError):
            verify_detached(DATA, packet(2, body), read_certs(good))
    with pytest.raises(OpenPgpError):  # a subpacket of length 0
        verify_detached(DATA, packet(2, b'\x04\x00\x16\x08\x00\x01\x00\x00\x00\x00\x00'), read_certs(good))
    long_sub = subpacket(20, b'n' * 300)
    assert verify_detached(DATA, detached(key, DATA, hashed=[long_sub]), read_certs(good))
    five = b'\xff' + (2).to_bytes(4, 'big') + b'\x14n'
    assert verify_detached(DATA, detached(key, DATA, hashed=[five]), read_certs(good))
    # oversized signature values never verify
    rsa_key = PgpKey('rsa')
    rsa_certs = read_certs(cert(rsa_key))
    rsa_parsed = openpgp.parse_signature(signature(rsa_key, DATA, 0))
    assert rsa_parsed is not None
    oversized = rsa_parsed.hashed_part + b'\x00\x00\x00\x00' + (2056).to_bytes(2, 'big') + b'\x01' * 257
    with pytest.raises(OpenPgpError):
        verify_detached(DATA, packet(2, oversized), rsa_certs)
    parsed = openpgp.parse_signature(signature(key, DATA, 0))
    assert parsed is not None
    too_long = parsed.hashed_part + b'\x00\x00' + b'\x00\x00' + b'\x01\x08' + b'\x01' * 33 + b'\x01\x00' + b'\x01' * 32
    with pytest.raises(OpenPgpError):
        verify_detached(DATA, packet(2, too_long), read_certs(good))


def fixture_body(url: str) -> bytes:
    """A recorded upstream body by URL (a GitHub release asset is recorded under its redirect target)."""
    host, name = url.split('/')[2], url.rsplit('/', 1)[-1]
    folder = FIXTURES / ('release-assets.githubusercontent.com' if host == 'github.com' else host)
    for meta in sorted(folder.glob('*.json')):
        recorded = str(json.loads(meta.read_text())['request']['url'])
        if recorded == url or f'filename%3D{name}&' in recorded:
            return meta.with_suffix('.body').read_bytes()
    raise AssertionError(f'no recorded body for {url}')


@pytest.mark.parametrize('origin', ['registry.terraform.io', 'registry.opentofu.org'])
def test_real_registry_signatures(origin: str) -> None:
    """The recorded download document's keys verify the recorded SHA256SUMS signature (HashiCorp signs with an RSA
    subkey whose binding was renewed after the signature was made; OpenTofu with an RSA primary key)."""
    doc = json.loads(next((FIXTURES / origin).glob('get-amd64-*.body')).read_bytes())
    sums = fixture_body(doc['shasums_url'])
    sig = fixture_body(doc['shasums_signature_url'])
    certs = [c for k in doc['signing_keys']['gpg_public_keys'] for c in read_certs(k['ascii_armor'].encode())]
    found = verify_detached(sums, sig, certs)
    assert found.cert_fingerprint in {c.fingerprint for c in certs}
    with pytest.raises(OpenPgpError):
        verify_detached(sums + b'\n', sig, certs)
