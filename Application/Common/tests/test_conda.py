"""Conda names, VersionOrder and the streaming repodata reader (``waystation_common.conda``)."""

import bz2
import itertools
import json
import random
from pathlib import Path

import pytest
from ws_conda import repodata as make_repodata

from waystation_common import conda
from waystation_common.cleanup.plan import coordinates
from waystation_common.cleanup.versions import is_prerelease, sort_key
from waystation_common.components import key_for
from waystation_common.conda import repodata
from waystation_common.versionsort import version_sort

# conda's documented order (conda.models.version.VersionOrder); '==' pairs compare equal.
ORDER = [
    ['0.4', '0.4.0'],
    ['0.4.1.rc', '0.4.1.RC'],
    ['0.4.1'],
    ['0.5a1'],
    ['0.5b3'],
    ['0.5C1'],
    ['0.5'],
    ['0.9.6'],
    ['0.960923'],
    ['1.0'],
    ['1.1dev1'],
    ['1.1_'],
    ['1.1a1'],
    ['1.1.0dev1', '1.1.dev1'],
    ['1.1.a1'],
    ['1.1.0rc1'],
    ['1.1.0', '1.1'],
    ['1.1.0post1', '1.1.post1'],
    ['1.1post1'],
    ['1996.07.12'],
    ['1!0.4.1'],
    ['1!3.1.1.6.0.1'],
    ['2!0.4.1'],
]


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def test_version_order_matches_conda() -> None:
    flat = [(rank, v) for rank, group in enumerate(ORDER) for v in group]
    for (ra, a), (rb, b) in itertools.product(flat, flat):
        expected = _sign(ra - rb)
        assert conda.compare(a, b) == expected, (a, b)
        sa, sb = conda.sort_string(a), conda.sort_string(b)
        assert _sign((sa > sb) - (sa < sb)) == expected, (a, b)
        assert version_sort('conda', a) == sa


def test_sort_string_agrees_with_compare_on_random_versions() -> None:
    rng = random.Random(7)
    parts = ['0', '1', '2', '10', 'a', 'b1', 'rc2', 'post1', 'dev3', '_', '01']
    versions = ['.'.join(rng.choice(parts) for _ in range(rng.randint(1, 4))) for _ in range(300)]
    versions += ['1.0+local.1', '1.0+local.2', '1.0', '2!1.0', '1.0-1']
    for a, b in itertools.combinations(versions, 2):
        if conda.parse_version(a) is None or conda.parse_version(b) is None:
            continue
        sa, sb = conda.sort_string(a), conda.sort_string(b)
        assert _sign((sa > sb) - (sa < sb)) == conda.compare(a, b), (a, b)


def test_invalid_versions_sort_first() -> None:
    assert conda.parse_version('1.0 beta') is None and conda.parse_version('1!x!2') is None
    assert conda.parse_version('a!1') is None and conda.parse_version('1+a+b') is None
    assert conda.compare('not valid?', '0.1') == -1 and conda.compare('0.1', '??') == 1
    assert conda.sort_string('??') < conda.sort_string('0')
    assert conda.newest(['1.0', '1.10', '1.9']) == '1.10' and conda.newest([]) is None


def test_prerelease_and_cleanup_order() -> None:
    assert conda.is_prerelease('2.0rc1') and conda.is_prerelease('1.0.dev3') and conda.is_prerelease('1.0b2')
    assert not conda.is_prerelease('1.0') and not conda.is_prerelease('1.1.post1') and not conda.is_prerelease('?')
    assert not conda.is_prerelease('2025b') and conda.is_prerelease('2.0dev')
    assert is_prerelease('conda', '3.0a1') and not is_prerelease('conda', '3.0')
    assert sorted(['1.10', '1.9', '1.9post1', '1.10rc1'], key=lambda v: sort_key('conda', v)) == [
        '1.9',
        '1.9post1',
        '1.10rc1',
        '1.10',
    ]


def test_file_names_and_paths() -> None:
    found = conda.parse_filename('python-dateutil-2.9.0.post0-pyhd8ed1ab_1.conda')
    assert found is not None and (found.name, found.version, found.build, found.suffix) == (
        'python-dateutil',
        '2.9.0.post0',
        'pyhd8ed1ab_1',
        '.conda',
    )
    assert found.filename == 'python-dateutil-2.9.0.post0-pyhd8ed1ab_1.conda'
    assert conda.parse_filename('tzdata-2024b-hc8b5060_0.tar.bz2') is not None
    for bad in ('tzdata-2024b.conda', 'Tzdata-2024b-h_0.conda', 'tzdata-2024b-h_0.zip', 'x-1 0-h_0.conda'):
        assert conda.parse_filename(bad) is None, bad
    assert conda.parse_path('noarch/tzdata-2024b-hc8b5060_0.conda') is not None
    assert conda.parse_path('../tzdata-2024b-hc8b5060_0.conda') is None
    assert conda.parse_path('noarch/repodata.json') is None
    assert all(conda.valid_subdir(s) for s in ('noarch', 'linux-64', 'osx-arm64', 'emscripten-wasm32', 'win-64'))
    assert not any(conda.valid_subdir(s) for s in ('', 'Linux-64', 'linux-64-x', '_ws', 'a/b', '..'))


def test_components_and_cleanup_coordinates() -> None:
    path = 'linux-64/numpy-2.1.3-py312h58c1407_0.conda'
    key = key_for('conda', path)
    assert key is not None and (key.name, key.version) == ('numpy', '2.1.3')
    assert key.version_sort == conda.sort_string('2.1.3')
    assert key_for('conda', 'linux-64/repodata.json') is None
    coord = coordinates('conda', path)
    assert (coord.name, coord.version, coord.group, coord.ordered_by_version) == (
        'numpy',
        '2.1.3',
        'numpy/linux-64',
        True,
    )
    assert coordinates('conda', 'linux-64/repodata.json').group == 'linux-64/repodata.json'


# ---------------------------------------------------------------------------- repodata reader
def _entries() -> dict[str, dict[str, object]]:
    good = {'name': 'demo', 'version': '1.0', 'build': 'py_0', 'build_number': 3, 'sha256': 'a' * 64, 'size': 10}
    return {
        'demo-1.0-py_0.tar.bz2': {**good, 'md5': 'b' * 32, 'timestamp': 1_700_000_000_000},
        'demo-1.0-py_0.conda': {**good, 'sha256': 'c' * 64},
        'demo-2.0-py_0.conda': {**good, 'version': '2.0', 'sha256': None, 'md5': 'd' * 32},
        'demo-3.0-py_0.conda': {**good, 'version': '9.9'},  # name/version disagree with the file name
        'demo-4.0-py_0.conda': {**good, 'version': '4.0', 'sha256': 'XYZ'},
        'demo-5.0-py_0.conda': {**good, 'version': '5.0', 'sha256': None},  # no checksum at all
        '../evil-1.0-0.conda': {**good, 'name': 'evil'},
        'demo-6.0-py_0.tar.bz2.conda': {**good},
    }


@pytest.mark.parametrize('compression', ['json', 'zst', 'bz2'])
def test_read_entries_streams_every_encoding(tmp_path: Path, compression: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from compression import zstd

    monkeypatch.setattr(repodata, 'CHUNK', 7)  # values cross chunk boundaries everywhere
    raw = make_repodata('noarch', _entries())
    data = {'json': raw, 'zst': zstd.compress(raw), 'bz2': bz2.compress(raw)}[compression]
    path = tmp_path / 'repodata'
    path.write_bytes(data)
    result = repodata.Scan()
    found = list(repodata.read_entries(path, compression, result))
    assert [e.filename for e in found] == ['demo-1.0-py_0.tar.bz2', 'demo-1.0-py_0.conda', 'demo-2.0-py_0.conda']
    assert found[0].md5 == 'b' * 32 and found[0].timestamp == 1_700_000_000_000 and found[0].build_number == 3
    assert found[2].sha256 is None and found[2].md5 == 'd' * 32
    assert result.subdir == 'noarch' and result.entries == 8 and result.skipped == 5
    assert result.extra == {'repodata_version': 1}
    assert [len(b) for b in repodata.batches(iter(found), 2)] == [2, 1]


@pytest.mark.parametrize(
    'body',
    [b'', b'[]', b'{"packages": {"a": }', b'{"info": {}} trailing', b'{"packages": {"x.conda": {}}', b'{1: 2}'],
)
def test_malformed_repodata_is_refused(tmp_path: Path, body: bytes) -> None:
    path = tmp_path / 'r.json'
    path.write_bytes(body)
    with pytest.raises(repodata.RepodataError):
        list(repodata.read_entries(path, 'json', repodata.Scan()))


def test_repodata_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / 'r.json'
    path.write_bytes(make_repodata('noarch', _entries()))
    with pytest.raises(repodata.RepodataError, match='too large'):
        list(repodata.read_entries(path, 'json', repodata.Scan(), limit=100))
    with pytest.raises(repodata.RepodataError, match='too many'):
        list(repodata.scan(iter([path.read_text()]), repodata.Scan(), max_entries=2))
    monkeypatch.setattr(repodata, 'MAX_VALUE', 16)
    monkeypatch.setattr(repodata, 'CHUNK', 4)
    with pytest.raises(repodata.RepodataError, match='too large'):
        list(repodata.read_entries(path, 'json', repodata.Scan()))
    broken = tmp_path / 'r.json.zst'
    broken.write_bytes(b'not zstd')
    with pytest.raises(repodata.RepodataError):
        list(repodata.read_entries(broken, 'zst', repodata.Scan()))
    assert repodata.compression_of('noarch/repodata.json.bz2') == 'bz2'
    assert repodata.compression_of('noarch/current_repodata.json') == 'json'


def test_render_and_channeldata() -> None:
    doc = json.loads(repodata.render('noarch', {'b-1-0.conda': {'name': 'b'}, 'a-1-0.tar.bz2': {'name': 'a'}}))
    assert doc['info'] == {'subdir': 'noarch'} and list(doc['packages']) == ['a-1-0.tar.bz2']
    assert list(doc['packages.conda']) == ['b-1-0.conda'] and doc['repodata_version'] == 1
    assert json.loads(repodata.empty('linux-64'))['packages.conda'] == {}
    channel = json.loads(repodata.channeldata({'a': {'subdirs': ['noarch'], 'version': '1'}}, ['noarch', 'linux-64']))
    assert channel['subdirs'] == ['linux-64', 'noarch'] and channel['packages']['a']['version'] == '1'
    from compression import zstd

    assert zstd.decompress(repodata.compress_zst(b'{}')) == b'{}'
