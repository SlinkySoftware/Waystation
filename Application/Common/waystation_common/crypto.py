"""AES-256-GCM encryption at rest for stored secrets (ARCHITECTURE §7).

``WAYSTATION_SECRET_KEYS`` holds one or more base64 32-byte keys. The **first** key encrypts; **every** key can
decrypt, so a new key is rolled out by prepending it, re-encrypting (``Keyring.rotate``), then removing the old key.

Token format (ASCII, safe to store as a string field)::

    ws1.<kid>.<base64url(nonce[12] || ciphertext || tag[16])>

``kid`` is the first 8 hex digits of ``sha256('waystation-kid' || key)``: it selects the key without revealing it,
and lets ``needs_rotation`` spot tokens made with an old key. Pass ``aad`` (associated data, e.g. ``b'repo:<id>'``)
to bind a ciphertext to where it is stored; the same ``aad`` is required to decrypt.

Usage::

    ring = Keyring.from_settings(settings.secret_keys)
    token = ring.encrypt_str('upstream-password', aad=b'repo:01J...')
    ring.decrypt_str(token, aad=b'repo:01J...')
    if ring.needs_rotation(token):
        token = ring.rotate(token, aad=b'repo:01J...')
"""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Iterable, Sequence

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from waystation_common.errors import CryptoError

PREFIX = 'ws1'
KEY_BYTES = 32
NONCE_BYTES = 12


def key_id(key: bytes) -> str:
    return hashlib.sha256(b'waystation-kid' + key).hexdigest()[:8]


def parse_key(value: str) -> bytes:
    """Decode one base64 (standard or url-safe) 32-byte key."""
    text = value.strip().replace('-', '+').replace('_', '/').rstrip('=')
    try:
        raw = base64.b64decode(text + '=' * (-len(text) % 4), validate=True)
    except ValueError:  # binascii.Error, or non-ASCII input
        raise CryptoError('secret key is not valid base64') from None
    if len(raw) != KEY_BYTES:
        raise CryptoError(f'secret key must be {KEY_BYTES} bytes, got {len(raw)}')
    return raw


def generate_key() -> str:
    """A new random key, base64 encoded (for docs, tests and dev env files)."""
    return base64.b64encode(os.urandom(KEY_BYTES)).decode('ascii')


class Keyring:
    def __init__(self, keys: Sequence[bytes]) -> None:
        if not keys:
            raise CryptoError('no secret keys configured (WAYSTATION_SECRET_KEYS)')
        for key in keys:
            if len(key) != KEY_BYTES:
                raise CryptoError(f'secret key must be {KEY_BYTES} bytes')
        self._keys = [(key_id(key), AESGCM(key)) for key in keys]

    @classmethod
    def from_strings(cls, values: Iterable[str]) -> Keyring:
        return cls([parse_key(value) for value in values])

    @classmethod
    def from_settings(cls, values: Iterable[SecretStr]) -> Keyring:
        return cls.from_strings(value.get_secret_value() for value in values)

    @property
    def active_kid(self) -> str:
        return self._keys[0][0]

    def encrypt(self, plaintext: bytes, *, aad: bytes = b'') -> str:
        kid, aead = self._keys[0]
        nonce = os.urandom(NONCE_BYTES)
        sealed = aead.encrypt(nonce, plaintext, aad or None)
        body = base64.urlsafe_b64encode(nonce + sealed).rstrip(b'=').decode('ascii')
        return f'{PREFIX}.{kid}.{body}'

    def decrypt(self, token: str, *, aad: bytes = b'') -> bytes:
        kid, blob = _split(token)
        candidates = [aead for k, aead in self._keys if k == kid]
        if not candidates:
            raise CryptoError('ciphertext was made with an unknown key')
        nonce, sealed = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        for aead in candidates:
            try:
                return aead.decrypt(nonce, sealed, aad or None)
            except InvalidTag:
                continue
        raise CryptoError('ciphertext failed authentication')

    def encrypt_str(self, plaintext: str, *, aad: bytes = b'') -> str:
        return self.encrypt(plaintext.encode('utf-8'), aad=aad)

    def decrypt_str(self, token: str, *, aad: bytes = b'') -> str:
        return self.decrypt(token, aad=aad).decode('utf-8')

    def needs_rotation(self, token: str) -> bool:
        """True when ``token`` was not made with the active (first) key."""
        return _split(token)[0] != self.active_kid

    def rotate(self, token: str, *, aad: bytes = b'') -> str:
        """Re-encrypt with the active key; returns ``token`` unchanged when it already uses it."""
        if not self.needs_rotation(token):
            return token
        return self.encrypt(self.decrypt(token, aad=aad), aad=aad)


def _split(token: str) -> tuple[str, bytes]:
    parts = token.split('.')
    if len(parts) != 3 or parts[0] != PREFIX:
        raise CryptoError('not a Waystation ciphertext')
    body = parts[2]
    try:
        blob = base64.b64decode(body + '=' * (-len(body) % 4), altchars=b'-_', validate=True)
    except ValueError:  # binascii.Error, or non-ASCII input
        raise CryptoError('ciphertext is not valid base64') from None
    if len(blob) < NONCE_BYTES + 16:
        raise CryptoError('ciphertext too short')
    return parts[1], blob
