"""Shared helpers of the apk plugin (proxy indexing and hosted uploads)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def details(fields: dict[str, str]) -> dict[str, Any]:
    """Browse details of an index stanza."""
    built = fields.get('t', '')
    depends = fields.get('D', '').split()
    return {
        'description': fields.get('T', '')[:500] or None,
        'origin': fields.get('o', '')[:200] or None,
        'license': fields.get('L', '')[:200] or None,
        'url': fields.get('U', '')[:500] or None,
        'installed_size': int(fields['I']) if _number(fields.get('I', '')) else None,
        'build_time': datetime.fromtimestamp(int(built), UTC) if _number(built) and len(built) <= 11 else None,
        'depends': depends[:200],
    }


def _number(value: str) -> bool:
    """An ASCII decimal that fits MongoDB's 64-bit integers (``str.isdigit`` also accepts ``²`` and other digits
    ``int`` refuses)."""
    return value.isascii() and value.isdigit() and len(value) <= 18
