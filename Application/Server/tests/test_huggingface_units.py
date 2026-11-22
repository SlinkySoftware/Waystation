"""Hugging Face (T-P6-HF) units: routes, keys, info documents, Xet stripping, git blob checks, prefetch items,
options, components and cleanup coordinates."""

import hashlib
import io
import json

import pytest
from pydantic import ValidationError

from waystation_common.cleanup.plan import coordinates
from waystation_common.components import key_for
from waystation_common.errors import DigestMismatch, UpstreamError
from waystation_common.huggingface import file_key, parse_display_name, parse_file_key, valid_repo_id, valid_revision
from waystation_server.formats.huggingface import prefetch
from waystation_server.formats.huggingface.descriptor import DESCRIPTOR, HuggingfaceOptions, host_allowed
from waystation_server.formats.huggingface.metadata import (
    git_blob_check,
    parse_info,
    rewrite_link,
    strip_xet,
    validate_tree,
)
from waystation_server.formats.huggingface.paths import (
    Op,
    Route,
    info_key,
    info_upstream,
    parse_route,
    resolve_upstream,
    tree_key,
    tree_query,
)
from waystation_server.formats.huggingface.plugin import hub_headers
from waystation_server.proxy.engine import UpstreamInfo

C1 = 'a' * 40
SHA = 'b' * 64
INFO = UpstreamInfo(url='https://huggingface.co/x', status=200, headers={}, latency_ms=1)


@pytest.mark.parametrize(
    ('path', 'route'),
    [
        ('api/models/gpt2', Route(Op.INFO, 'model', 'gpt2', None)),
        ('api/models/org/m', Route(Op.INFO, 'model', 'org/m', None)),
        ('api/models/gpt2/revision/main', Route(Op.INFO, 'model', 'gpt2', 'main')),
        ('api/models/org/m/revision/v1.0', Route(Op.INFO, 'model', 'org/m', 'v1.0')),
        ('api/models/org/m/revision/refs/pr/3', Route(Op.INFO, 'model', 'org/m', 'refs/pr/3')),
        ('api/datasets/org/d/revision/' + C1, Route(Op.INFO, 'dataset', 'org/d', C1)),
        ('api/models/org/m/tree/main', Route(Op.TREE, 'model', 'org/m', 'main', '')),
        ('api/models/gpt2/tree/main/onnx/sub', Route(Op.TREE, 'model', 'gpt2', 'main', 'onnx/sub')),
        ('org/m/resolve/main/config.json', Route(Op.RESOLVE, 'model', 'org/m', 'main', 'config.json')),
        ('gpt2/resolve/main/onnx/model.onnx', Route(Op.RESOLVE, 'model', 'gpt2', 'main', 'onnx/model.onnx')),
        ('org/m/resolve/refs/pr/1/a.bin', Route(Op.RESOLVE, 'model', 'org/m', 'refs/pr/1', 'a.bin')),
        (
            'datasets/org/d/resolve/' + C1 + '/data/x.parquet',
            Route(Op.RESOLVE, 'dataset', 'org/d', C1, 'data/x.parquet'),
        ),
    ],
)
def test_routes(path: str, route: Route) -> None:
    assert parse_route(path) == route


@pytest.mark.parametrize(
    'path',
    [
        '',
        'api',
        'api/spaces/org/s/revision/main',
        'api/models/org/m/revision',
        'api/whoami-v2',
        'api/models/org/m/xet-read-token/main',
        'org/m/resolve/main',
        'org/m/blob/main/config.json',
        '-bad/m/resolve/main/x',
        'org/m/resolve/bad rev/x',
        'a/b/c/resolve/main/x',
    ],
)
def test_not_routes(path: str) -> None:
    assert parse_route(path) is None


def test_names_and_keys() -> None:
    assert valid_repo_id('openai-community/gpt2') and valid_repo_id('gpt2')
    assert not valid_repo_id('a/b/c') and not valid_repo_id('a--b') and not valid_repo_id('a..b')
    assert valid_revision('refs/convert/parquet') and not valid_revision('feature/x') and not valid_revision('x.lock')
    key = file_key('dataset', 'org/d', C1, 'data/x.parquet')
    assert key == f'datasets/org/d/resolve/{C1}/data/x.parquet'
    found = parse_file_key(key)
    assert found is not None and (found.kind, found.repo_id, found.commit, found.path, found.name) == (
        'dataset',
        'org/d',
        C1,
        'data/x.parquet',
        'datasets/org/d',
    )
    assert parse_file_key(f'gpt2/resolve/{C1}/config.json') is not None
    assert parse_file_key('gpt2/resolve/main/config.json') is None
    assert parse_file_key('api/models/gpt2/revision/main') is None
    assert parse_display_name('datasets/org/d') == ('dataset', 'org/d')
    assert parse_display_name('a/b/c') is None
    assert info_key('model', 'org/m', None) == 'api/models/org/m'
    assert info_upstream('model', 'org/m', 'refs/pr/1') == 'api/models/org/m/revision/refs%2Fpr%2F1?blobs=true'
    assert resolve_upstream('model', 'org/m', C1, 'dir/a b.json') == f'org/m/resolve/{C1}/dir/a%20b.json'
    query = tree_query(True, False, 'abc/=', None)
    assert query == 'recursive=true&expand=false&cursor=abc%2F%3D'
    assert tree_key(Route(Op.TREE, 'model', 'org/m', 'main', 'sub'), query) == f'api/models/org/m/tree/main/sub?{query}'


def info_doc(**extra: object) -> bytes:
    doc = {
        'id': 'org/m',
        'sha': C1,
        'siblings': [
            {'rfilename': 'config.json', 'blobId': 'c' * 40, 'size': 10},
            {'rfilename': 'model.bin', 'blobId': 'd' * 40, 'size': 5, 'lfs': {'sha256': SHA, 'size': 99}},
            {'rfilename': '../evil', 'blobId': 'e' * 40, 'size': 1},
            {'rfilename': 'nosize.json', 'blobId': 'e' * 40},
            {'rfilename': 'badlfs.bin', 'lfs': {'sha256': 'nope', 'size': 1}, 'size': 1},
        ],
        **extra,
    }
    return json.dumps(doc).encode()


def test_parse_info() -> None:
    revision = parse_info(info_doc())
    assert revision.commit == C1 and revision.listed == 5
    assert set(revision.files) == {'config.json', 'model.bin'}
    config, model = revision.files['config.json'], revision.files['model.bin']
    assert (config.lfs, config.etag, config.size) == (False, 'c' * 40, 10)
    assert (model.lfs, model.etag, model.size) == (True, SHA, 99)
    assert hub_headers(C1, model) == {
        'X-Repo-Commit': C1,
        'ETag': f'"{SHA}"',
        'X-Linked-Etag': f'"{SHA}"',
        'X-Linked-Size': '99',
    }
    assert revision.size == 109
    for bad in (b'nope', b'[]', json.dumps({'sha': 'main'}).encode(), json.dumps({'sha': C1, 'siblings': 3}).encode()):
        with pytest.raises(UpstreamError):
            parse_info(bad)
    assert parse_info(json.dumps({'sha': C1}).encode()).files == {}


def test_strip_xet_and_tree() -> None:
    plain = info_doc()
    assert strip_xet(plain, INFO) is plain
    tree = json.dumps([{'type': 'file', 'path': 'a', 'xetHash': 'f' * 64, 'lfs': {'oid': SHA}}]).encode()
    stripped = json.loads(strip_xet(tree, INFO))
    assert stripped == [{'type': 'file', 'path': 'a', 'lfs': {'oid': SHA}}]
    validate_tree(tree, INFO)
    with pytest.raises(UpstreamError):
        validate_tree(b'{}', INFO)


def test_git_blob_check() -> None:
    content = b'{"hello": "world"}\n'
    blob_id = hashlib.sha1(b'blob %d\0' % len(content) + content, usedforsecurity=False).hexdigest()
    git_blob_check(blob_id, len(content))(io.BytesIO(content))
    with pytest.raises(DigestMismatch):
        git_blob_check(blob_id, len(content))(io.BytesIO(content.upper()))


def test_rewrite_link() -> None:
    link = '<https://huggingface.co/api/models/org/m/tree/main?cursor=x>; rel="next"'
    assert rewrite_link(link, 'https://huggingface.co', 'https://ws.example.com/repository/hf/') == (
        '<https://ws.example.com/repository/hf/api/models/org/m/tree/main?cursor=x>; rel="next"'
    )
    assert rewrite_link('</api/x>; rel="next"', 'https://huggingface.co/', 'https://ws.example.com/repository/hf/') == (
        '<https://ws.example.com/repository/hf/api/x>; rel="next"'
    )


def test_prefetch_items() -> None:
    assert prefetch.parse_item('org/m') == ('model', 'org/m', 'main', None)
    assert prefetch.parse_item('gpt2@v1.0:onnx/model.onnx') == ('model', 'gpt2', 'v1.0', 'onnx/model.onnx')
    assert prefetch.parse_item('datasets/org/d@refs/convert/parquet') == (
        'dataset',
        'org/d',
        'refs/convert/parquet',
        None,
    )
    for bad in ('', 'a/b/c', 'org/m@bad rev', 'org/m@feature/x', 'org/m:../x', 'org/m:'):
        assert prefetch.check_item(bad) is not None, bad
    assert prefetch.check_item('org/m@main:config.json') is None
    assert prefetch.refresh_request('api/models/org/m/revision/main') is not None
    assert prefetch.refresh_request('api/models/org/m') is not None
    assert prefetch.refresh_request(f'api/models/org/m/revision/{C1}') is None
    assert prefetch.refresh_request('api/models/org/m/tree/main?recursive=true&expand=false') is None
    assert prefetch.refresh_request(f'org/m/resolve/{C1}/config.json') is None


def test_options_and_hosts() -> None:
    options = HuggingfaceOptions()
    assert options.cdn_hosts == ['huggingface.co', '*.huggingface.co', '*.hf.co']
    assert host_allowed('us.aws.cdn.hf.co', options.cdn_hosts) and host_allowed('cdn-lfs.hf.co', options.cdn_hosts)
    assert not host_allowed('hf.co.example.com', options.cdn_hosts)
    assert host_allowed('a.cdn.test', ['*.cdn.test']) and not host_allowed('badcdn.test', ['*.cdn.test'])
    assert HuggingfaceOptions(cdn_hosts=['CDN.Example.com', 'cdn.example.com']).cdn_hosts == ['cdn.example.com']
    with pytest.raises(ValidationError):
        HuggingfaceOptions(cdn_hosts=['http://x'])
    with pytest.raises(ValidationError):
        HuggingfaceOptions.model_validate({'other': 1})
    assert {t.value for t in DESCRIPTOR.types} == {'proxy'}


def test_components_and_cleanup() -> None:
    key = key_for('huggingface', f'org/m/resolve/{C1}/model.bin')
    assert key is not None and (key.namespace, key.name, key.version) == ('', 'org/m', C1)
    assert key.display == f'org/m@{C1[:12]}'
    dataset = key_for('huggingface', f'datasets/org/d/resolve/{C1}/x.parquet')
    assert dataset is not None and dataset.display == f'datasets/org/d@{C1[:12]}'
    assert key_for('huggingface', 'api/models/org/m/revision/main') is None
    assert key_for('huggingface', f'org/m/resolve/{C1}/model.bin', content=False) is None
    coord = coordinates('huggingface', f'org/m/resolve/{C1}/sub/a.bin')
    assert (coord.name, coord.version, coord.group, coord.ordered_by_version) == ('org/m', C1, 'org/m', False)
