"""Composer names, version normalisation, stability, order and minified metadata (``waystation_common.composer``)."""

import pytest

from waystation_common import composer
from waystation_common.cleanup.plan import coordinates
from waystation_common.cleanup.versions import is_prerelease, sort_key
from waystation_common.components import key_for
from waystation_common.versionsort import version_sort

N_10_0_0_0 = '10.0.0.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_1_0_0_0 = '1.0.0.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_1_0_1_0 = '1.0.1.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_1_2_0_0 = '1.2.0.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_1_2_3_0 = '1.2.3.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_1_2_3_4 = '1.2.3.4'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_2_0_0_0 = '2.0.0.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_2_0_2_0 = '2.0.2.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_2_1_3_0 = '2.1.3.0'  # forbidden-strings: allow (a normalised Composer version, not an address)


@pytest.mark.parametrize(
    ('given', 'expected'),
    [
        ('Monolog/Monolog', 'monolog/monolog'),
        ('psr/http-message', 'psr/http-message'),
        ('symfony/polyfill-php80', 'symfony/polyfill-php80'),
        ('a/b--c', 'a/b--c'),
        ('a.b/c_d', 'a.b/c_d'),
        ('nope', None),
        ('a/b/c', None),
        ('-a/b', None),
        ('a/b-', None),
        ('a/../b', None),
        ('a/' + 'b' * 300, None),
    ],
)
def test_names(given: str, expected: str | None) -> None:
    assert composer.normalise_name(given) == expected


@pytest.mark.parametrize(
    ('given', 'expected'),
    [
        ('1.2.3', N_1_2_3_0),
        ('v1.2', N_1_2_0_0),
        (N_1_2_3_4, N_1_2_3_4),
        ('1.0-beta2', f'{N_1_0_0_0}-beta2'),
        ('1.0.0-b1', f'{N_1_0_0_0}-beta1'),
        ('2.0.0-RC1', f'{N_2_0_0_0}-RC1'),
        ('2.0.0-rc.1', f'{N_2_0_0_0}-RC1'),
        ('1.0.0-alpha.1.2', f'{N_1_0_0_0}-alpha1.2'),
        ('2.1.3-p1', f'{N_2_1_3_0}-patch1'),
        ('1.0.0-stable', N_1_0_0_0),
        ('1.0.0+build.5', N_1_0_0_0),
        ('1.0-dev', f'{N_1_0_0_0}-dev'),
        ('1.x-dev', '1.9999999.9999999.9999999-dev'),
        ('1.0.x-dev', '1.0.9999999.9999999-dev'),
        ('dev-main', 'dev-main'),
        ('dev-feature/Login', 'dev-feature/Login'),
        ('master', 'dev-master'),
        ('20240115', '20240115'),
        ('2024.01.15', '2024.01.15.0'),
        ('20240115.1', '20240115.1'),
        ('foo', None),
        ('', None),
        ('1.0 beta', None),
        ('dev-', None),
    ],
)
def test_normalise(given: str, expected: str | None) -> None:
    assert composer.normalise(given) == expected


@pytest.mark.parametrize(
    ('version', 'stability'),
    [
        ('1.0.0', 'stable'),
        (f'{N_1_0_0_0}-patch1', 'stable'),
        ('1.0.0-beta2', 'beta'),
        ('1.0.0-b2', 'beta'),
        ('1.0.0-alpha', 'alpha'),
        ('1.0.0-RC1', 'RC'),
        (f'{N_1_0_0_0}-dev', 'dev'),
        ('dev-main', 'dev'),
        ('1.x-dev', 'dev'),
    ],
)
def test_stability(version: str, stability: str) -> None:
    assert composer.stability(version) == stability
    assert composer.is_prerelease(version) == (stability != 'stable')


def test_order() -> None:
    ordered = [
        'dev-feature',
        'dev-main',
        f'{N_1_0_0_0}-dev',
        f'{N_1_0_0_0}-alpha1',
        f'{N_1_0_0_0}-beta1-dev',
        f'{N_1_0_0_0}-beta1',
        f'{N_1_0_0_0}-beta2',
        f'{N_1_0_0_0}-beta10',
        f'{N_1_0_0_0}-RC1',
        N_1_0_0_0,
        f'{N_1_0_0_0}-patch1',
        N_1_0_1_0,
        '1.9999999.9999999.9999999-dev',
        N_2_0_0_0,
        N_10_0_0_0,
    ]
    shuffled = list(reversed(ordered))
    assert sorted(shuffled, key=composer.sort_string) == ordered
    assert composer.sort_string('v1.2') == composer.sort_string(N_1_2_0_0)
    assert sorted(shuffled, key=lambda v: sort_key('composer', v)) == ordered
    assert sorted(shuffled, key=lambda v: version_sort('composer', v)) == ordered
    assert is_prerelease('composer', '1.0.0-beta1') and not is_prerelease('composer', '1.0.0')


def test_newest_and_dev() -> None:
    assert composer.newest([N_1_0_0_0, f'{N_2_0_0_0}-beta1', 'dev-main']) == N_1_0_0_0
    assert composer.newest([f'{N_2_0_0_0}-beta1', 'dev-main']) == f'{N_2_0_0_0}-beta1'
    assert composer.newest(['dev-main']) == 'dev-main'
    assert composer.newest([]) is None
    assert composer.is_dev('dev-main') and composer.is_dev('1.x-dev') and not composer.is_dev(N_1_0_0_0)


def test_path_segment() -> None:
    assert composer.path_segment(N_1_2_3_0) == N_1_2_3_0
    assert composer.path_segment('dev-main') == 'dev-main'
    hashed = composer.path_segment('dev-feature/login')
    assert hashed.startswith('dev-feature_login-') and '/' not in hashed
    assert composer.path_segment(hashed) == hashed
    assert composer.path_segment('dev-..') != 'dev-..'


def test_minify_round_trip() -> None:
    expanded = [
        {'name': 'a/b', 'version': '2.0.0', 'require': {'php': '>=8.1'}, 'dist': {'url': 'u2'}},
        {'name': 'a/b', 'version': '1.0.0', 'require': {'php': '>=8.1'}, 'dist': {'url': 'u1'}, 'bin': ['x']},
        {'name': 'a/b', 'version': '0.9.0', 'dist': {'url': 'u0'}},
    ]
    minified = composer.minify(expanded)
    assert minified[1] == {'version': '1.0.0', 'dist': {'url': 'u1'}, 'bin': ['x']}
    assert minified[2] == {'version': '0.9.0', 'dist': {'url': 'u0'}, 'require': '__unset', 'bin': '__unset'}
    assert composer.expand(minified) == expanded
    assert composer.expand(['junk', *minified]) == expanded


def test_cleanup_and_components() -> None:
    path = f'dists/psr/container/{N_2_0_2_0}/c71ecc56dfe541dbd90c5360474fbc405f8d5963.zip'
    coord = coordinates('composer', path)
    assert (coord.name, coord.version, coord.ordered_by_version) == ('psr/container', N_2_0_2_0, True)
    key = key_for('composer', path)
    assert key is not None and key.name == 'psr/container' and key.version == N_2_0_2_0
    assert key_for('composer', 'p2/psr/container.json') is None
