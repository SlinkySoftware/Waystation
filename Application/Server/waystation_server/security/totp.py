"""TOTP (RFC 6238) and recovery codes for local users' two-factor sign-in (SPECIFICATION §4.9), standard library only.

- HOTP (RFC 4226) with HMAC-SHA1, 6 digits, 30-second steps from the Unix epoch: what every authenticator app
  understands from an ``otpauth://totp/`` URI.
- ``match_step`` accepts the current step and one either side (clock drift), compares in constant time and always
  computes every candidate, so the time taken does not depend on which one matched. It returns the matched step;
  the caller makes each step usable once per user (``security.second_factor``).
- Secrets are 160 random bits (RFC 4226 §4), shown to the user once as base32 (no padding) and stored encrypted.
- Recovery codes are 10 random characters (Crockford-style base32 without look-alikes, about 50 bits), shown as
  ``xxxxx-xxxxx``; only a keyed hash (HMAC-SHA256 under a key derived from ``WAYSTATION_SECRET_KEYS``, bound to the
  user id) is stored, so a database dump does not reveal them and they cannot be checked offline without the key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct
from collections.abc import Sequence
from urllib.parse import quote, urlencode

STEP_S = 30
DIGITS = 6
DRIFT_STEPS = 1
SECRET_BYTES = 20
RECOVERY_CODES = 10
RECOVERY_LENGTH = 10
RECOVERY_ALPHABET = '23456789abcdefghjkmnpqrstuvwxyz'  # no 0/o, 1/l/i
RECOVERY_INFO = b'waystation totp recovery code v1'
_CODE = re.compile(r'^[0-9]{6}$')


def new_secret() -> str:
    """A fresh shared secret, base32 without padding (32 characters)."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode('ascii').rstrip('=')


def secret_bytes(secret: str) -> bytes:
    text = secret.strip().replace(' ', '').upper()
    return base64.b32decode(text + '=' * (-len(text) % 8))


def step_at(now: float) -> int:
    return int(now // STEP_S)


def hotp(key: bytes, counter: int) -> str:
    """RFC 4226 HOTP value of ``counter`` (dynamic truncation, ``DIGITS`` digits)."""
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack('>I', digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def looks_like_code(value: str) -> bool:
    """Six digits (spaces ignored): a TOTP code rather than a recovery code."""
    return bool(_CODE.fullmatch(value.replace(' ', '').strip()))


def match_step(secret: str, code: str, now: float) -> int | None:
    """The step (current ±``DRIFT_STEPS``) whose code is ``code``, or None. Constant time over the candidates."""
    presented = code.replace(' ', '').strip()
    if not _CODE.fullmatch(presented):
        return None
    key = secret_bytes(secret)
    current = step_at(now)
    matched: int | None = None
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        if hmac.compare_digest(hotp(key, step).encode(), presented.encode()) and matched is None:
            matched = step
    return matched


def otpauth_uri(secret: str, *, account: str, issuer: str) -> str:
    """``otpauth://totp/<issuer>:<account>?secret=...&issuer=...`` (the Key Uri Format authenticator apps read)."""
    label = quote(f'{issuer}:{account}', safe='@:')
    query = urlencode(
        {'secret': secret, 'issuer': issuer, 'algorithm': 'SHA1', 'digits': DIGITS, 'period': STEP_S},
        quote_via=quote,
    )
    return f'otpauth://totp/{label}?{query}'


# ---------------------------------------------------------------------------- recovery codes
def new_recovery_codes() -> list[str]:
    """``RECOVERY_CODES`` fresh codes, formatted ``xxxxx-xxxxx``."""
    out: list[str] = []
    while len(out) < RECOVERY_CODES:
        raw = ''.join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_LENGTH))
        code = f'{raw[:5]}-{raw[5:]}'
        if code not in out:
            out.append(code)
    return out


def normalise_recovery(code: str) -> str | None:
    """The canonical form of a typed recovery code (case, spaces and dashes ignored), or None when it cannot be one."""
    raw = re.sub(r'[\s-]', '', code).lower()
    if len(raw) != RECOVERY_LENGTH or any(ch not in RECOVERY_ALPHABET for ch in raw):
        return None
    return raw


class RecoveryHasher:
    """HMAC-SHA256 of ``<user id>:<code>`` under keys derived from the secret keys (first = current; every key is
    tried when checking, so rotating the keys does not invalidate issued codes)."""

    def __init__(self, keys: Sequence[bytes]) -> None:
        if not keys:
            raise ValueError('recovery codes need WAYSTATION_SECRET_KEYS')
        self._keys = [hmac.new(key, RECOVERY_INFO, hashlib.sha256).digest() for key in keys]

    def hash(self, user_id: str, code: str) -> str:
        return self.candidates(user_id, code)[0]

    def candidates(self, user_id: str, code: str) -> list[str]:
        raw = normalise_recovery(code) or ''
        data = f'{user_id}:{raw}'.encode()
        return [hmac.new(key, data, hashlib.sha256).hexdigest() for key in self._keys]
