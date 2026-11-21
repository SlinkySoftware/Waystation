"""RPM version ordering (``rpmvercmp`` and EVR comparison) as a sortable string: ``version_sort`` of
``yum_packages`` (browse lists versions newest first with a plain index sort).

``rpmvercmp`` splits a version into runs of digits and runs of letters (every other character only separates
them); numeric runs compare as numbers and are newer than alphabetic runs; alphabetic runs compare as bytes; ``~``
sorts before anything, even the end of the string (``1.0~rc1 < 1.0``); ``^`` sorts after the end of the string but
before any further segment (``1.0 < 1.0^git1 < 1.0.1``). An EVR compares epoch (numeric), then version, then
release.

The key encodes each element with a marker that orders as above: ``!`` tilde, ``#`` end, ``%`` caret, ``.`` an
alphabetic run (its letters follow, all greater than every marker so a shorter run sorts first), ``/`` a numeric run
(leading zeros dropped, then two-digit length, then the digits, so longer numbers sort later).
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r'~|\^|[0-9]+|[A-Za-z]+')
MAX_DIGITS = 99


def version_key(value: str) -> str:
    """The sortable form of one version or release string."""
    out: list[str] = []
    for part in _TOKEN.findall(value):
        if part == '~':
            out.append('!')
        elif part == '^':
            out.append('%')
        elif part.isdigit():
            digits = part.lstrip('0') or '0'
            digits = digits[:MAX_DIGITS] if len(digits) > MAX_DIGITS else digits
            out.append(f'/{len(digits):02d}{digits}')
        else:
            out.append('.' + part)
    out.append('#')
    return ''.join(out)


def evr_key(epoch: str | int | None, version: str, release: str) -> str:
    """``version_sort`` of a package: epoch, version and release keys."""
    text = str(epoch if epoch is not None else '').strip()
    number = int(text) if text.isdigit() else 0
    return f'{min(number, 10**10 - 1):010d}:{version_key(version)}:{version_key(release)}'


def evr_string(epoch: str | int | None, version: str, release: str) -> str:
    """``[epoch:]version-release`` as dnf prints it (epoch shown when not 0)."""
    text = str(epoch if epoch is not None else '').strip()
    prefix = f'{text}:' if text and text != '0' else ''
    return f'{prefix}{version}-{release}' if release else f'{prefix}{version}'
