"""TOTP and recovery codes (T-O08): RFC 4226/6238 test vectors, drift window, otpauth URI, recovery code hashing."""

import base64
from urllib.parse import parse_qs, urlsplit

import pytest

from waystation_server.security import totp

RFC_KEY = b'12345678901234567890'
RFC_SECRET = base64.b32encode(RFC_KEY).decode().rstrip('=')


def test_hotp_matches_rfc4226_appendix_d() -> None:
    expected = ['755224', '287082', '359152', '969429', '338314', '254676', '287922', '162583', '399871', '520489']
    assert [totp.hotp(RFC_KEY, counter) for counter in range(10)] == expected


@pytest.mark.parametrize(
    ('now', 'code'),
    [
        # RFC 6238 Appendix B (SHA-1), the last six of the eight digits.
        (59, '287082'),
        (1111111109, '081804'),
        (1111111111, '050471'),
        (1234567890, '005924'),
        (2000000000, '279037'),
        (20000000000, '353130'),
    ],
)
def test_totp_matches_rfc6238_appendix_b(now: int, code: str) -> None:
    assert totp.hotp(totp.secret_bytes(RFC_SECRET), totp.step_at(now)) == code
    assert totp.match_step(RFC_SECRET, code, now) == totp.step_at(now)


def test_one_step_of_drift_either_way() -> None:
    now = 1_700_000_000.0
    step = totp.step_at(now)
    key = totp.secret_bytes(RFC_SECRET)
    assert totp.match_step(RFC_SECRET, totp.hotp(key, step - 1), now) == step - 1
    assert totp.match_step(RFC_SECRET, totp.hotp(key, step + 1), now) == step + 1
    assert totp.match_step(RFC_SECRET, totp.hotp(key, step - 2), now) is None
    assert totp.match_step(RFC_SECRET, totp.hotp(key, step + 2), now) is None


def test_malformed_codes_never_match() -> None:
    now = 59
    for code in ('', '28708', '2870822', 'abcdef', '287 08x', '\uff12\uff18\uff17\uff10\uff18\uff12'):
        assert totp.match_step(RFC_SECRET, code, now) is None
    assert totp.match_step(RFC_SECRET, ' 287 082 ', now) == 1  # spaces are ignored


def test_new_secret_is_160_bits_of_base32() -> None:
    secret = totp.new_secret()
    assert len(secret) == 32 and secret.isalnum() and secret == secret.upper()
    assert len(totp.secret_bytes(secret)) == 20
    assert totp.new_secret() != secret


def test_otpauth_uri() -> None:
    uri = totp.otpauth_uri('JBSWY3DPEHPK3PXP', account='ana@example.com', issuer='Lab mirror')
    parts = urlsplit(uri)
    assert parts.scheme == 'otpauth' and parts.netloc == 'totp'
    assert parts.path == '/Lab%20mirror:ana@example.com'
    query = parse_qs(parts.query)
    assert query == {
        'secret': ['JBSWY3DPEHPK3PXP'],
        'issuer': ['Lab mirror'],
        'algorithm': ['SHA1'],
        'digits': ['6'],
        'period': ['30'],
    }


def test_looks_like_code() -> None:
    assert totp.looks_like_code('123456') and totp.looks_like_code('123 456')
    assert not totp.looks_like_code('abcde-fghjk') and not totp.looks_like_code('1234567')


def test_recovery_codes_are_unique_and_well_formed() -> None:
    codes = totp.new_recovery_codes()
    assert len(codes) == totp.RECOVERY_CODES == len(set(codes))
    for code in codes:
        assert len(code) == 11 and code[5] == '-'
        assert totp.normalise_recovery(code) == code.replace('-', '')


def test_recovery_code_normalisation() -> None:
    assert totp.normalise_recovery(' ABCDE-FGHJK ') == 'abcdefghjk'
    assert totp.normalise_recovery('abcde fghjk') == 'abcdefghjk'
    assert totp.normalise_recovery('abcde-fghj') is None  # too short
    assert totp.normalise_recovery('abcde-fghj0') is None  # 0 is not in the alphabet
    assert totp.normalise_recovery('') is None


def test_recovery_hash_is_keyed_bound_to_the_user_and_survives_key_rotation() -> None:
    old, new = b'\x01' * 32, b'\x02' * 32
    before = totp.RecoveryHasher([old])
    stored = before.hash('user-1', 'abcde-fghjk')
    assert stored == before.hash('user-1', 'ABCDEFGHJK')
    assert stored != before.hash('user-2', 'abcde-fghjk')
    assert stored != totp.RecoveryHasher([new]).hash('user-1', 'abcde-fghjk')
    rotated = totp.RecoveryHasher([new, old])
    assert stored in rotated.candidates('user-1', 'abcde-fghjk')
    assert stored not in rotated.candidates('user-1', 'abcde-fghjm')


def test_recovery_hasher_needs_keys() -> None:
    with pytest.raises(ValueError, match='SECRET_KEYS'):
        totp.RecoveryHasher([])
