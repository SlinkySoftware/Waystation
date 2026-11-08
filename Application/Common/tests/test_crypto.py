import base64

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import SecretStr

from waystation_common.crypto import KEY_BYTES, Keyring, generate_key, key_id, parse_key
from waystation_common.errors import CryptoError

K1 = generate_key()
K2 = generate_key()


def test_round_trip_and_aad() -> None:
    ring = Keyring.from_strings([K1])
    token = ring.encrypt_str('hunter2', aad=b'repo:1')
    assert token.startswith(f'ws1.{ring.active_kid}.')
    assert 'hunter2' not in token
    assert ring.decrypt_str(token, aad=b'repo:1') == 'hunter2'
    with pytest.raises(CryptoError, match='authentication'):
        ring.decrypt(token, aad=b'repo:2')
    with pytest.raises(CryptoError, match='authentication'):
        ring.decrypt(token)


def test_no_aad_round_trip_and_nonce_is_random() -> None:
    ring = Keyring.from_strings([K1])
    a, b = ring.encrypt(b'x'), ring.encrypt(b'x')
    assert a != b
    assert ring.decrypt(a) == ring.decrypt(b) == b'x'


@given(st.binary(max_size=2048), st.binary(max_size=64))
def test_property_round_trip(plaintext: bytes, aad: bytes) -> None:
    ring = Keyring.from_strings([K1, K2])
    assert ring.decrypt(ring.encrypt(plaintext, aad=aad), aad=aad) == plaintext


def test_first_key_encrypts_all_decrypt_and_rotation() -> None:
    old = Keyring.from_strings([K1])
    token = old.encrypt_str('s', aad=b'ctx')
    new = Keyring.from_strings([K2, K1])
    assert new.decrypt_str(token, aad=b'ctx') == 's'
    assert new.needs_rotation(token)
    rotated = new.rotate(token, aad=b'ctx')
    assert rotated != token
    assert not new.needs_rotation(rotated)
    assert new.rotate(rotated, aad=b'ctx') == rotated  # no-op when current
    only_new = Keyring.from_strings([K2])
    assert only_new.decrypt_str(rotated, aad=b'ctx') == 's'
    with pytest.raises(CryptoError, match='unknown key'):
        only_new.decrypt(token)


def test_same_kid_collision_tries_each_candidate() -> None:
    ring = Keyring.from_strings([K1, K2])
    token = Keyring.from_strings([K2]).encrypt(b'v')
    # force both keys under one kid to exercise the candidate loop
    kid2 = ring._keys[1][0]
    ring._keys = [(kid2, ring._keys[0][1]), ring._keys[1]]
    assert ring.decrypt(token) == b'v'


def test_from_settings_and_key_formats() -> None:
    raw = bytes(range(KEY_BYTES))
    std = base64.b64encode(raw).decode()
    url = base64.urlsafe_b64encode(raw).decode().rstrip('=')
    assert parse_key(std) == parse_key(url) == parse_key(f'  {std}\n') == raw
    ring = Keyring.from_settings([SecretStr(std)])
    assert ring.active_kid == key_id(raw)


@pytest.mark.parametrize('bad', ['not base64!', base64.b64encode(b'short').decode(), 'é' * 44])
def test_parse_key_rejects(bad: str) -> None:
    with pytest.raises(CryptoError):
        parse_key(bad)


def test_keyring_rejects_empty_and_bad_length() -> None:
    with pytest.raises(CryptoError, match='no secret keys'):
        Keyring([])
    with pytest.raises(CryptoError, match='32 bytes'):
        Keyring([b'x' * 16])


@pytest.mark.parametrize(
    'token',
    [
        'garbage',
        'ws2.abcd1234.AAAA',
        'ws1.abcd1234',
        'ws1.a.b.c',
        'ws1.abcd1234.!!!!',
        'ws1.abcd1234.é',
        'ws1.abcd1234.' + base64.urlsafe_b64encode(b'x' * 27).decode(),
    ],
)
def test_malformed_tokens(token: str) -> None:
    ring = Keyring.from_strings([K1])
    with pytest.raises(CryptoError):
        ring.decrypt(token)


def test_tampered_ciphertext() -> None:
    ring = Keyring.from_strings([K1])
    token = ring.encrypt(b'payload')
    prefix, body = token.rsplit('.', 1)
    flipped = body[:-2] + ('A' if body[-2] != 'A' else 'B') + body[-1]
    with pytest.raises(CryptoError, match='authentication'):
        ring.decrypt(f'{prefix}.{flipped}')
