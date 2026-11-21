"""Substring search keys and query pieces (T-U06, adr/0009): name suffixes, the substring pattern, positions in a
substring stream and the two-phase keyset cursor."""

import re

import pytest

from waystation_common.searchkeys import SFX, file_keys, name_keys, suffixes
from waystation_server.db.search import Start, matching_keys, substring_pattern
from waystation_server.search import engine


def test_suffixes_start_after_the_first_character() -> None:
    assert suffixes('nginx') == ['ginx', 'inx']
    assert suffixes('aaaaa') == ['aaa', 'aaaa']
    assert suffixes('abc') == []
    assert suffixes('') == []
    assert suffixes('xüber') == ['ber', 'über']
    long = 'a' + 'b' * 40
    assert max(len(k) for k in suffixes(long)) == SFX
    assert suffixes(long) == ['b' * n for n in range(3, SFX + 1)]


def test_keys_carry_suffixes_of_the_folded_name() -> None:
    assert name_keys('Library/NGINX') == {
        'name_lc': 'library/nginx',
        'name_sfx': suffixes('library/nginx'),
        'base_lc': 'nginx',
    }
    assert name_keys('go') == {'name_lc': 'go', 'name_sfx': []}
    assert file_keys('scripts/NGINX-Setup.sh') == {'name_lc': 'nginx-setup.sh', 'name_sfx': suffixes('nginx-setup.sh')}


def test_matching_keys_are_in_index_order() -> None:
    assert matching_keys('a-setup-setup.sh', 'setup') == ['setup-setup.sh', 'setup.sh']
    assert matching_keys('setup.sh', 'setup') == []  # at the start only: a prefix match
    term = 'x' * (SFX + 4)
    assert matching_keys('a' + term, term) == ['x' * SFX]  # longer terms: the first SFX characters


def _matches(term: str, name: str, *, has_base: bool) -> bool:
    pattern = substring_pattern(term, has_base=has_base)
    assert pattern.flags & re.S
    return re.search(pattern.pattern, name, re.S) is not None


@pytest.mark.parametrize(
    ('term', 'name', 'has_base', 'expected'),
    [
        ('setup', 'nginx-setup.sh', False, True),
        ('setup', 'setup.sh', False, False),  # a prefix match: found by the prefix stream
        ('nginx', 'library/nginx', True, False),  # its last segment starts with it: the base_lc stream has it
        ('nginx', 'library/nginx', False, True),  # no base_lc stream for this source: found here
        ('gin', 'library/nginx', True, True),
        ('lib', 'library/nginx', True, False),
        ('ngi', 'a/nginx/b', True, True),  # not the last segment
        ('a.b', 'xaxb', False, False),  # the term is literal
        ('a.b', 'xa.b', False, True),
        ('(x', 'y(x', False, True),
        ('-x', 'a\n-x', False, True),
        ('oo', 'foo/oo', True, False),
    ],
)
def test_substring_pattern(term: str, name: str, has_base: bool, expected: bool) -> None:
    assert _matches(term, name, has_base=has_base) is expected


def test_start_positions() -> None:
    after = Start('setup.sh', repo_id='r2', id='b')
    assert after.precedes('setup.sh', 'r2', 'c')
    assert after.precedes('setup.sh', 'r3', 'a')
    assert after.precedes('setup.zip', 'r1', 'a')
    assert not after.precedes('setup.sh', 'r2', 'b')
    assert not after.precedes('setup.sh', 'r1', 'z')
    assert after.admits('setup.sh', 'r2', 'b')
    assert not after.admits('setup.ab', 'r9', 'z')
    assert Start('k', inclusive=True).precedes('k', 'r', 'a')
    assert not Start('k').precedes('k', 'r', 'a')
    assert Start('k').precedes('l', 'r', 'a')


def test_cursor_phases_put_prefix_matches_first() -> None:
    prefix_rank, substring_rank = 0, engine.SUBSTRING_RANK
    assert all(sub for _, _, sub in engine.STREAMS[substring_rank:])
    assert not any(sub for _, _, sub in engine.STREAMS[:substring_rank])
    in_prefix = ('nginx', prefix_rank, 'repo', 'sabc')
    in_substring = ('a-nginx', substring_rank, 'repo', 'sabc')
    # cursor among the prefix matches: substring streams start from the beginning
    assert engine._start(in_prefix, substring_rank + 1) is None
    # cursor among the substring matches: prefix streams are done
    assert engine._start(in_substring, prefix_rank) is engine._DONE
    same = engine._start(in_substring, substring_rank)
    assert same == Start('a-nginx', repo_id='repo', id='abc')
    assert engine._start(in_substring, substring_rank + 1) == Start('a-nginx', inclusive=True)
