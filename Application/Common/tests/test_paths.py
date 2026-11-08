import posixpath
import unicodedata
from urllib.parse import quote, unquote

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from waystation_common.errors import PathRejected
from waystation_common.paths import MAX_PATH_BYTES, MAX_SEGMENT_BYTES, normalise


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('a/b/c', 'a/b/c'),
        ('a//b/', 'a/b'),
        ('./a/./b', 'a/b'),
        ('pool/main/n/nginx/nginx_1.26.0-1_amd64.deb', 'pool/main/n/nginx/nginx_1.26.0-1_amd64.deb'),
        ('@scope/pkg/-/pkg-1.0.0.tgz', '@scope/pkg/-/pkg-1.0.0.tgz'),
        ('a/C:/b', 'a/C:/b'),
        ('...', '...'),
        ('a/..b/c..', 'a/..b/c..'),
        ('caf\u0065\u0301', 'caf\u00e9'),  # NFC
        ('100%', '100%'),
        ('a%20b', 'a%20b'),  # not decoded unless asked
    ],
)
def test_accepts_and_normalises(raw: str, expected: str) -> None:
    assert normalise(raw) == expected


@pytest.mark.parametrize(
    ('raw', 'decoded'),
    [
        ('a%20b/c', 'a b/c'),
        ('%40scope%2Fpkg', '@scope/pkg'),
        ('caf%C3%A9', 'caf\u00e9'),
        ('a%2520b', 'a%20b'),
        ('a%252e', None),
    ],
)
def test_decode(raw: str, decoded: str | None) -> None:
    if decoded is None:
        with pytest.raises(PathRejected):
            normalise(raw, decode=True)
    else:
        assert normalise(raw, decode=True) == decoded


@pytest.mark.parametrize(
    'raw',
    [
        '..',
        '../etc/passwd',
        'a/../../b',
        'a/..',
        '/etc/passwd',
        '//server/share',
        'C:/Windows',
        'c:foo',
        './C:x',
        'a\\b',
        '..\\..\\x',
        'a\x00b',
        'a\nb',
        'a\x7fb',
        'a/%2e%2e/b',
        'a/%2E%2e',
        'a%2fb',
        'a%5cb',
        'a%00',
        'a/\u2025/b',  # TWO DOT LEADER folds to '..'
        'a/\u2024',  # ONE DOT LEADER folds to '.'
        'a/\uff0e\uff0e/b',  # fullwidth dots
        'a\uff0fb',  # fullwidth solidus folds to '/'
        'a\ud800b',  # lone surrogate
        '',
        '/',
        './/.',
        'x' * (MAX_SEGMENT_BYTES + 1),
        'é' * (MAX_SEGMENT_BYTES // 2 + 1),
        '/'.join(['abcdefgh'] * (MAX_PATH_BYTES // 9 + 1)),
    ],
)
def test_rejects(raw: str) -> None:
    with pytest.raises(PathRejected):
        normalise(raw)


@pytest.mark.parametrize('raw', ['%2e%2e/x', '..%2fx', '%2F%2Fx', '%2fetc', '%5c..', '%00', '%ff', 'a%c3', '%e2%80%a5'])
def test_rejects_encoded(raw: str) -> None:
    with pytest.raises(PathRejected):
        normalise(raw, decode=True)


def test_allow_empty() -> None:
    assert normalise('', allow_empty=True) == ''
    assert normalise('./', allow_empty=True) == ''


def test_segment_and_path_limits_are_inclusive() -> None:
    assert normalise('x' * MAX_SEGMENT_BYTES) == 'x' * MAX_SEGMENT_BYTES
    long_path = '/'.join(['x' * 200] * (MAX_PATH_BYTES // 201))
    assert normalise(long_path) == long_path


def _check_safe(result: str) -> None:
    assert not result.startswith('/')
    assert '\x00' not in result
    assert '\\' not in result
    segments = result.split('/')
    for segment in segments:
        assert segment not in ('', '.', '..')
        assert unicodedata.normalize('NFKC', segment) not in ('.', '..')
    assert posixpath.normpath(result) == result
    assert unicodedata.is_normalized('NFC', result)
    assert normalise(result) == result  # idempotent


@given(st.text(max_size=300))
@example('a/../b')
@example('a/./b//c/')
def test_property_raw(raw: str) -> None:
    try:
        result = normalise(raw)
    except PathRejected:
        return
    _check_safe(result)


segment_chars = st.sampled_from(['a', 'Z', '0', '.', '..', '/', '\\', '%', '%2e', '%2f', '\x00', 'é', '\u2025', ' '])


@given(st.lists(segment_chars, max_size=30).map(''.join))
def test_property_adversarial_alphabet(raw: str) -> None:
    for decode in (False, True):
        try:
            result = normalise(raw, decode=decode)
        except PathRejected:
            continue
        _check_safe(result)
        # whatever comes out never resolves above its root, even if decoded again by someone else
        assert '..' not in unquote(result).split('/')


@given(st.lists(st.text(st.characters(codec='utf-8', exclude_characters='/'), min_size=1, max_size=20), max_size=8))
def test_property_encoded_round_trip(segments: list[str]) -> None:
    raw = '/'.join(quote(s, safe='') for s in segments)
    try:
        result = normalise(raw, decode=True)
    except PathRejected:
        return
    _check_safe(result)
    assert result == '/'.join(unicodedata.normalize('NFC', s) for s in segments if s not in ('', '.'))
