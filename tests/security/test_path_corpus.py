"""Path corpus (T-R01, Documents/Security/ASVS_L2.md V12.3) against ``waystation_common.paths.normalise``, the one
path normalisation function every repository-relative path goes through (the format router calls it before any
plugin sees a path; ``Application/Server/tests/test_security_matrix.py`` checks that over HTTP).

Whatever goes in, what comes out is a relative POSIX path with no ``.``/``..`` segment, no control character, no
backslash, no drive letter and no encoded separator, that stays inside any directory it is joined to and is stable
when normalised again.
"""

from __future__ import annotations

import posixpath
import unicodedata

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from waystation_common.errors import PathRejected
from waystation_common.paths import normalise

TRAVERSAL = [
    '..',
    '../etc/passwd',
    'a/../../etc/passwd',
    'a/b/../../..',
    '/etc/passwd',
    '//etc/passwd',
    'C:/Windows/win.ini',
    'c:',
    'a\\..\\..\\b',
    '..\\b',
    '%2e%2e/etc/passwd',
    '%2E%2E%2Fetc%2Fpasswd',
    '..%2f..%2fetc',
    '%252e%252e/etc',  # double encoding (decoded once: %2e%2e stays and is refused)
    '%c0%ae%c0%ae/etc',  # overlong UTF-8 dot
    '%e0%80%ae%e0%80%ae/',
    'a%00b',
    'a\x00b',
    'a\nb',
    'a\rb',
    'a\x7fb',
    '\u2025/etc',  # two dot leader, NFKC → ..
    '\uff0e\uff0e/etc',  # fullwidth full stops
    'a\uff0fb/../x',  # fullwidth solidus inside a segment
    'a\u2215..',  # division slash
    'a/\ufe52\ufe52/b',  # small full stop
    '\ud800',  # lone surrogate
    'x' * 256,
    ('a/' * 2100) + 'b',
    '%',
    '%zz',
    '%ff',
]


@pytest.mark.parametrize('decode', [False, True])
@pytest.mark.parametrize('path', TRAVERSAL)
def test_traversal_and_encoding_tricks_are_refused_or_contained(path: str, decode: bool) -> None:
    try:
        clean = normalise(path, decode=decode)
    except PathRejected:
        return
    _assert_safe(clean)


ACCEPTED = {
    'a//./b/': 'a/b',
    './a': 'a',
    'pkg/-/pkg-1.0.0.tgz': 'pkg/-/pkg-1.0.0.tgz',
    '@scope/name': '@scope/name',
    'a..b/c...': 'a..b/c...',  # dots inside a name are fine
    '.hidden/x': '.hidden/x',
    'cafe\u0301': 'caf\u00e9',  # NFC
}


@pytest.mark.parametrize(('path', 'expected'), ACCEPTED.items())
def test_ordinary_names_survive(path: str, expected: str) -> None:
    assert normalise(path) == expected


def _assert_safe(clean: str) -> None:
    assert clean and not clean.startswith('/')
    assert '\\' not in clean and '\x00' not in clean
    segments = clean.split('/')
    for segment in segments:
        assert segment not in ('', '.', '..')
        assert unicodedata.normalize('NFKC', segment) not in ('.', '..')
        assert '/' not in unicodedata.normalize('NFKC', segment)
    assert not (
        len(segments[0]) >= 2 and segments[0][0].isascii() and segments[0][0].isalpha() and segments[0][1] == ':'
    )
    joined = posixpath.normpath(posixpath.join('/base', clean))
    assert joined.startswith('/base/'), joined
    assert normalise(clean) == clean


_ALPHABET = st.sampled_from(
    ['a', 'b', '.', '..', '/', '\\', '%', '2e', '2f', '5c', '00', ':', 'C', '\x00', '\u2025', '\uff0e', '\uff0f', '~']
)


@settings(max_examples=3000, deadline=None)
@given(st.lists(_ALPHABET, max_size=24).map(''.join), st.booleans())
def test_fuzzed_paths_are_refused_or_safe(path: str, decode: bool) -> None:
    try:
        clean = normalise(path, decode=decode)
    except PathRejected:
        return
    _assert_safe(clean)
