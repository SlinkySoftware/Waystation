"""Local passwords: argon2id (SPECIFICATION §4.9). Hashing costs ~50 ms of CPU, so it always runs in a thread."""

from __future__ import annotations

import asyncio
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id with the library's RFC 9106 "low memory" profile
# Verified against when the user does not exist, so timing does not reveal valid usernames.
_DUMMY = _hasher.hash(secrets.token_urlsafe(16))


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


def _verify(stored: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored or _DUMMY, password) and stored is not None
    except VerifyMismatchError, VerificationError, InvalidHashError:
        return False


async def verify_password(stored: str | None, password: str) -> bool:
    """True when ``password`` matches ``stored``; ``stored=None`` (unknown user) costs the same and is False."""
    return await asyncio.to_thread(_verify, stored, password)


def needs_rehash(stored: str) -> bool:
    return _hasher.check_needs_rehash(stored)


def generate_password() -> str:
    """A one-time password (bootstrap admin)."""
    return secrets.token_urlsafe(18)
