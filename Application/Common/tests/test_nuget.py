"""NuGet versions and ids (T-P6-NUGET): parsing, normalisation, NuGet order as a sort string, and how cleanup and the
component index read NuGet package paths."""

import functools
import random

import pytest

from waystation_common import nuget
from waystation_common.cleanup.plan import coordinates
from waystation_common.cleanup.versions import is_prerelease, sort_key
from waystation_common.components import ComponentKey, key_for
from waystation_common.versionsort import version_sort


@pytest.mark.parametrize(
    ('text', 'normalised', 'flat'),
    [
        ('1', '1.0.0', '1.0.0'),
        ('1.0', '1.0.0', '1.0.0'),
        ('1.0.0.0', '1.0.0', '1.0.0'),  # forbidden-strings: allow
        ('1.2.3.4', '1.2.3.4', '1.2.3.4'),  # forbidden-strings: allow
        ('01.002.0003', '1.2.3', '1.2.3'),
        ('1.0.0-Beta.1', '1.0.0-Beta.1', '1.0.0-beta.1'),
        ('1.0.0-rc.1+Build.5', '1.0.0-rc.1+Build.5', '1.0.0-rc.1'),
        ('13.0.3', '13.0.3', '13.0.3'),
    ],
)
def test_normalise(text: str, normalised: str, flat: str) -> None:
    assert nuget.normalise_version(text) == normalised
    assert nuget.flat_version(text) == flat


@pytest.mark.parametrize(
    'bad', ['', 'v1.0.0', '1.0.0.0.0', '1..0', '1.0-', '1.0+', 'latest', '1.0.0-beta_1', 'x' * 300]
)
def test_invalid_versions(bad: str) -> None:
    assert nuget.parse_version(bad) is None
    assert nuget.flat_version(bad) is None


def test_semver2_and_prerelease() -> None:
    assert nuget.parse_version('1.0.0-beta.1').is_semver2  # type: ignore[union-attr]
    assert nuget.parse_version('1.0.0+abc').is_semver2  # type: ignore[union-attr]
    assert not nuget.parse_version('1.0.0-beta1').is_semver2  # type: ignore[union-attr]
    assert nuget.is_prerelease('1.0.0-alpha') and not nuget.is_prerelease('1.0.0+meta')


ORDERED = [
    '0.9',
    '1.0.0-alpha',
    '1.0.0-ALPHA.1',
    '1.0.0-alpha.2',
    '1.0.0-alpha.10',
    '1.0.0-alpha.beta',
    '1.0.0-beta',
    '1.0.0-rc.1',
    '1.0.0',
    '1.0.0.1',  # forbidden-strings: allow
    '1.0.1',
    '1.2.0',
    '1.10.0',
    '10.0.0',
]


def test_sort_string_is_nuget_order() -> None:
    shuffled = ORDERED[:]
    random.Random(7).shuffle(shuffled)
    assert sorted(shuffled, key=nuget.sort_string) == ORDERED
    assert sorted(shuffled, key=functools.cmp_to_key(nuget.compare)) == ORDERED
    assert nuget.compare('1.0.0+a', '1.0.0+b') == 0  # metadata is ignored
    assert nuget.compare('1.0.0-BETA', '1.0.0-beta') == 0  # labels are case-insensitive
    assert nuget.sort_string('not-a-version') < nuget.sort_string('0.0.0')


def test_ids() -> None:
    for good in ('Newtonsoft.Json', 'a', 'System.Text.Json', 'my_pkg-2'):
        assert nuget.valid_id(good)
    for bad in ('', '.hidden', 'a..b', 'a/b', 'a b', 'x' * 101, 'trailing.'):
        assert not nuget.valid_id(bad)
    assert nuget.package_file('Newtonsoft.Json', '13.0.3') == 'newtonsoft.json/13.0.3/newtonsoft.json.13.0.3.nupkg'


def test_versionsort_cleanup_and_components() -> None:
    shuffled = ORDERED[:]
    random.Random(3).shuffle(shuffled)
    assert sorted(shuffled, key=lambda v: version_sort('nuget', v)) == ORDERED
    assert sorted(shuffled, key=lambda v: sort_key('nuget', v)) == ORDERED
    assert is_prerelease('nuget', '2.0.0-preview.1') and not is_prerelease('nuget', '2.0.0')
    path = 'v3-flatcontainer/newtonsoft.json/13.0.3/newtonsoft.json.13.0.3.nupkg'
    coord = coordinates('nuget', path)
    assert (coord.name, coord.version, coord.group, coord.ordered_by_version) == (
        'newtonsoft.json',
        '13.0.3',
        'newtonsoft.json',
        True,
    )
    nuspec = coordinates('nuget', 'v3-flatcontainer/newtonsoft.json/13.0.3/newtonsoft.json.nuspec')
    assert nuspec.version == '13.0.3'  # shares the version's rank
    assert not coordinates('nuget', 'v3-flatcontainer/newtonsoft.json/index.json').ordered_by_version
    assert key_for('nuget', path) == ComponentKey('nuget', '', 'newtonsoft.json', '13.0.3')
    assert key_for('nuget', 'v3-flatcontainer/newtonsoft.json/13.0.3/newtonsoft.json.nuspec') is None
    assert key_for('nuget', 'v3-flatcontainer/newtonsoft.json/index.json', content=False) is None
