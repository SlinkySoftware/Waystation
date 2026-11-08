"""Identifiers. Documents use ULIDs (sortable by creation time) as string ``_id`` (DATA_MODEL)."""

from __future__ import annotations

import re
from datetime import datetime

from ulid import ULID

_ULID_RE = re.compile(r'^[0-7][0-9A-HJKMNP-TV-Z]{25}$')


def new_id() -> str:
    """A new ULID string (26 chars, Crockford base32, upper case)."""
    return str(ULID())


def is_id(value: str) -> bool:
    return bool(_ULID_RE.fullmatch(value))


def id_time(value: str) -> datetime:
    """Creation time encoded in a ULID (UTC)."""
    return ULID.from_str(value).datetime
