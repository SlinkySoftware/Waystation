"""Helm format units (T-P5-HELM): index loading (safe YAML), the proxy rewrite, listings, group merges, versions,
chart package reading (tar-slip guards, limits), multipart parsing and "Set me up"."""

import gzip
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from waystation_common.errors import ValidationFailed
from waystation_server.formats.base import SetupContext
from waystation_server.formats.helm import chart, index
from waystation_server.formats.helm.descriptor import DESCRIPTOR
from waystation_server.formats.helm.hosted import parse_multipart
from waystation_server.repos.model import RepoConfig, RepoType, StoredGroup

UPSTREAM = 'https://charts.example.com/stable'
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
DIGEST = 'a' * 64

INDEX = f"""
apiVersion: v1
entries:
  web:
    - name: web
      version: 1.10.0
      appVersion: 1.10
      created: 2026-01-02T03:04:05.123456789Z
      digest: {DIGEST}
      deprecated: yes
      description: on
      urls: [charts/web-1.10.0.tgz]
      dependencies:
        - name: db
          version: ~1.0
          enabled: "true"
    - name: web
      version: 1.2.0
      urls: ["https://github.com/example/web/releases/download/web-1.2.0/web-1.2.0.tgz?x=1"]
      digest: not-a-digest
    - name: web
      version: 0.1.0
      urls: [../other/web-0.1.0.tgz]
    - name: web
      urls: [charts/no-version.tgz]
    - name: web
      version: 0.0.1
      urls: ["file:///etc/passwd"]
  "123":
    - version: 2
      urls: [https://user:pw@example.com/x.tgz]
generated: 2026-01-01T00:00:00Z
""".encode()


def test_version_sort_orders_by_semver() -> None:
    ordered = ['junk', '0.9.0', '1.0.0-alpha', '1.0.0-alpha.1', '1.0.0-alpha.beta', '1.0.0-beta.2', '1.0.0-beta.11']
    ordered += ['1.0.0-rc.1', '1.0.0', '1.2', '1.10.0', 'v2.0.0']
    keys = [index.version_sort(v) for v in ordered]
    assert keys == sorted(keys)
    assert index.version_sort('1.2') == index.version_sort('1.2.0')
    assert index.is_semver('v1.2.3-rc.1+build') and not index.is_semver('latest')


def test_rewrite_index_rewrites_urls_and_types() -> None:
    out = json.loads(index.rewrite_index(INDEX, upstream_url=UPSTREAM))
    assert list(out['entries']) == ['web']  # an entry without a usable URL is left out
    versions = {e['version']: e for e in out['entries']['web']}
    assert set(versions) == {'1.10.0', '1.2.0', '0.1.0'}
    first = versions['1.10.0']
    assert first['urls'] == ['charts/web-1.10.0.tgz']
    assert first['appVersion'] == '1.10'  # never read as the float 1.1
    assert first['deprecated'] is True and first['description'] == 'on'
    assert first['created'] == '2026-01-02T03:04:05.123456789Z'
    assert first['digest'] == DIGEST
    assert first['dependencies'] == [{'name': 'db', 'version': '~1.0', 'enabled': True}]
    assert versions['1.2.0']['urls'] == ['_ext/https/github.com/example/web/releases/download/web-1.2.0/web-1.2.0.tgz']
    assert 'digest' not in versions['1.2.0']
    assert versions['0.1.0']['urls'] == ['_ext/https/charts.example.com/other/web-0.1.0.tgz']
    assert out['generated'] == '2026-01-01T00:00:00Z'


def test_chart_paths_map_back_to_upstream_urls() -> None:
    assert index.chart_path('a b.tgz', UPSTREAM) == 'a b.tgz'
    assert index.url_of('a b.tgz') == 'a%20b.tgz'
    assert index.upstream_of('a b.tgz', UPSTREAM) == f'{UPSTREAM}/a%20b.tgz'
    ext = index.chart_path('http://other.example.com:8080/x/y.tgz', UPSTREAM)
    assert ext == '_ext/http/other.example.com:8080/x/y.tgz'
    assert index.upstream_of(ext, UPSTREAM) == 'http://other.example.com:8080/x/y.tgz'
    assert index.chart_path(f'{UPSTREAM}/_ext/https/evil.example.com/x.tgz', UPSTREAM) is not None
    assert index.chart_path(f'{UPSTREAM}/_ext/https/evil.example.com/x.tgz', UPSTREAM).startswith('_ext/https/charts')
    assert index.chart_path('index.yaml', UPSTREAM) == '_ext/https/charts.example.com/stable/index.yaml'
    assert index.chart_path('', UPSTREAM) is None
    assert index.chart_path('ftp://example.com/x.tgz', UPSTREAM) is None
    assert index.chart_path('x' * 3000, UPSTREAM) is None
    assert index.index_url(UPSTREAM) == f'{UPSTREAM}/index.yaml'


@pytest.mark.parametrize(
    'doc',
    [
        b'a: &a [1, 2]\nb: *a\n',
        b'!!python/object/apply:os.system ["true"]\n',
        b'entries: [unclosed\n',
        b'- just a list\n',
        b'entries: [1, 2]\n',
        b'[' * 200 + b']' * 200,
    ],
)
def test_unsafe_or_invalid_documents_are_refused(doc: bytes) -> None:
    with pytest.raises(ValidationFailed):
        index.rewrite_index(doc, upstream_url=UPSTREAM)


def test_billion_laughs_is_refused() -> None:
    lol = b'a: &a ["lol","lol"]\n' + b''.join(
        f'{chr(98 + i)}: &{chr(98 + i)} [*{chr(97 + i)},*{chr(97 + i)}]\n'.encode() for i in range(20)
    )
    with pytest.raises(ValidationFailed, match='aliases'):
        index.load_yaml(lol)


def test_json_indexes_are_read_directly() -> None:
    doc = {'apiVersion': 'v1', 'entries': {'x': [{'name': 'x', 'version': '1.0.0', 'urls': ['x-1.0.0.tgz']}]}}
    out = json.loads(index.rewrite_index(json.dumps(doc).encode(), upstream_url=UPSTREAM))
    assert out['entries']['x'][0]['urls'] == ['x-1.0.0.tgz']
    assert json.loads(index.rewrite_index(b'apiVersion: v1\n', upstream_url=UPSTREAM))['entries'] == {}


def test_listing_of_a_rewritten_index() -> None:
    listed = {item.version: item for item in index.listing(index.rewrite_index(INDEX, upstream_url=UPSTREAM))}
    first = listed['1.10.0']
    assert (first.name, first.path, first.digest, first.deprecated) == ('web', 'charts/web-1.10.0.tgz', DIGEST, True)
    assert first.created == datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    assert first.app_version == '1.10'
    record = first.record()
    assert record['version_sort'] == index.version_sort('1.10.0')
    assert listed['1.2.0'].digest is None


def test_merge_first_member_wins() -> None:
    first = index.dump({'entries': {'a': [{'name': 'a', 'version': '1.0.0', 'urls': ['one.tgz']}]}})
    second = index.dump(
        {
            'entries': {
                'a': [
                    {'name': 'a', 'version': '1.0.0', 'urls': ['two.tgz']},
                    {'name': 'a', 'version': '2.0.0', 'urls': ['a2.tgz']},
                ],
                'b': [{'name': 'b', 'version': '0.1.0', 'urls': ['b.tgz']}],
            }
        }
    )
    merged = json.loads(index.merge([first, second], now=NOW))
    assert [e['urls'][0] for e in merged['entries']['a']] == ['a2.tgz', 'one.tgz']
    assert list(merged['entries']) == ['a', 'b']
    assert json.loads(index.empty_index(NOW))['entries'] == {}


def test_parse_time() -> None:
    assert index.parse_time('2026-01-02T03:04:05Z') == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert index.parse_time('2026-01-02') == datetime(2026, 1, 2, tzinfo=UTC)
    assert index.parse_time('yesterday') is None
    assert index.parse_time(None) is None


# ---------------------------------------------------------------------------- chart packages
def package(tmp_path: Path, files: dict[str, bytes], *, extra: list[tarfile.TarInfo] | None = None) -> Path:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)
    path = tmp_path / 'chart.tgz'
    path.write_bytes(gzip.compress(buf.getvalue()))
    return path


CHART_YAML = b'apiVersion: v2\nname: demo\nversion: 0.1.0\nappVersion: "1.0"\ndescription: A demo\ndeprecated: false\n'


def test_read_chart(tmp_path: Path) -> None:
    meta = chart.read_chart(package(tmp_path, {'demo/Chart.yaml': CHART_YAML, 'demo/values.yaml': b'a: 1\n'}))
    assert (meta.name, meta.version, meta.filename) == ('demo', '0.1.0', 'demo-0.1.0.tgz')
    assert meta.entry['appVersion'] == '1.0' and meta.entry['deprecated'] is False
    assert meta.entry['apiVersion'] == 'v2'


@pytest.mark.parametrize(
    ('files', 'message'),
    [
        ({'demo/values.yaml': b''}, 'no <chart>/Chart.yaml'),
        ({'../demo/Chart.yaml': CHART_YAML}, 'unsafe'),
        ({'/demo/Chart.yaml': CHART_YAML}, 'unsafe'),
        ({'demo/Chart.yaml': CHART_YAML, 'other/x': b''}, 'single top-level'),
        ({'demo/Chart.yaml': b'name: demo\nversion: latest\n'}, 'SemVer'),
        ({'demo/Chart.yaml': b'name: ../x\nversion: 1.0.0\n'}, 'name'),
        ({'demo/Chart.yaml': b'apiVersion: v9\nname: demo\nversion: 1.0.0\n'}, 'apiVersion'),
        ({'demo/Chart.yaml': b'- a list\n'}, 'mapping'),
    ],
)
def test_bad_charts_are_refused(tmp_path: Path, files: dict[str, bytes], message: str) -> None:
    with pytest.raises(ValidationFailed, match=message):
        chart.read_chart(package(tmp_path, files))


def test_links_and_garbage_are_refused(tmp_path: Path) -> None:
    link = tarfile.TarInfo('demo/evil')
    link.type = tarfile.SYMTYPE
    link.linkname = '/etc/passwd'
    with pytest.raises(ValidationFailed, match='only files'):
        chart.read_chart(package(tmp_path, {'demo/Chart.yaml': CHART_YAML}, extra=[link]))
    junk = tmp_path / 'junk.tgz'
    junk.write_bytes(b'not a tarball')
    with pytest.raises(ValidationFailed, match='not a chart package'):
        chart.read_chart(junk)
    big = package(tmp_path, {'demo/Chart.yaml': b'#' * (chart.MAX_CHART_YAML + 1)})
    with pytest.raises(ValidationFailed, match='too large'):
        chart.read_chart(big)


def test_parse_multipart() -> None:
    body = (
        b'--XyZ\r\nContent-Disposition: form-data; name="chart"; filename="demo-0.1.0.tgz"\r\n'
        b'Content-Type: application/octet-stream\r\n\r\n\x1f\x8b\r\n\r\nbinary\r\n'
        b'--XyZ\r\nContent-Disposition: form-data; name="prov"\r\n\r\nsigned\r\n--XyZ--\r\n'
    )
    parts = parse_multipart('multipart/form-data; boundary=XyZ', body)
    assert parts == {'chart': b'\x1f\x8b\r\n\r\nbinary', 'prov': b'signed'}
    with pytest.raises(ValidationFailed):
        parse_multipart('multipart/form-data', body)


def repo(**fields: Any) -> RepoConfig:
    return RepoConfig.model_validate({'format': 'helm', 'created_at': NOW, 'updated_at': NOW, **fields})


def test_setup_snippets() -> None:
    def contents(repo: RepoConfig) -> str:
        ctx = SetupContext(repo=repo, base_url='https://ws.example.com', registry_host='ws.example.com', username='u')
        return '\n'.join(s.content for c in DESCRIPTOR.setup_snippets(ctx) for s in c.snippets)

    proxy = repo(id='r1', name='charts', type=RepoType.PROXY)
    text = contents(proxy)
    assert 'helm repo add charts https://ws.example.com/repository/charts/' in text
    assert '--upload-file' not in text
    hosted = contents(repo(id='r2', name='mine', type=RepoType.HOSTED))
    assert '--upload-file mychart-0.1.0.tgz https://ws.example.com/repository/mine/' in hosted
    assert '--context-path=/repository/mine' in hosted
    group = repo(id='r3', name='all', type=RepoType.GROUP, group=StoredGroup(members=('mine',), writable_member='mine'))
    assert 'cm-push' in contents(group)
