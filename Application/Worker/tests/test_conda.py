"""Hosted conda repodata rendering (``waystation_worker.conda.build``)."""

import json
from compression import zstd

from waystation_worker.conda import build


def _doc(subdir: str, name: str, version: str) -> dict[str, object]:
    filename = f'{name}-{version}-0.conda'
    entry = {'name': name, 'version': version, 'build': '0', 'subdir': subdir, 'license': f'L{version}'}
    return {'subdir': subdir, 'filename': filename, 'name': name, 'version': version, 'entry': entry}


def test_build_renders_every_subdir_and_noarch() -> None:
    files, summaries = build(
        [_doc('linux-64', 'demo', '1.9'), _doc('linux-64', 'demo', '1.10'), _doc('osx-64', 'x', '1')]
    )
    assert sorted(files) == [
        'channeldata.json',
        'linux-64/repodata.json',
        'linux-64/repodata.json.zst',
        'noarch/repodata.json',
        'noarch/repodata.json.zst',
        'osx-64/repodata.json',
        'osx-64/repodata.json.zst',
    ]
    linux = json.loads(files['linux-64/repodata.json'])
    assert sorted(linux['packages.conda']) == ['demo-1.10-0.conda', 'demo-1.9-0.conda']
    assert zstd.decompress(files['linux-64/repodata.json.zst']) == files['linux-64/repodata.json']
    assert json.loads(files['noarch/repodata.json'])['packages.conda'] == {}
    channel = json.loads(files['channeldata.json'])
    assert channel['packages']['demo'] == {'subdirs': ['linux-64'], 'version': '1.10', 'license': 'L1.10'}
    assert summaries['demo']['latest'] == '1.10' and summaries['demo']['counts'] == {'linux-64': 2}


def test_build_empty_channel() -> None:
    files, summaries = build([])
    assert sorted(files) == ['channeldata.json', 'noarch/repodata.json', 'noarch/repodata.json.zst']
    assert summaries == {}
