"""Maven format units (T-P5-MAVEN): version order, layout parsing, maven-metadata.xml, snapshot metadata, snippets."""

from datetime import UTC, datetime
from itertools import pairwise

import pytest

from waystation_common.errors import ValidationFailed
from waystation_server.formats.base import SetupContext
from waystation_server.formats.maven import coords
from waystation_server.formats.maven import metadata as md
from waystation_server.formats.maven.common import policy_allows
from waystation_server.formats.maven.descriptor import DESCRIPTOR, MavenOptions
from waystation_server.formats.maven.hosted import StoredFile, snapshot_metadata
from waystation_server.formats.maven.version import compare, is_snapshot, sort_key, sort_versions
from waystation_server.repos.model import RepoConfig, RepoType

# ---------------------------------------------------------------------------- version order
ASCENDING = [
    '1-alpha-1',
    '1-alpha-2',
    '1-alpha-10',
    '1-beta-1',
    '1-milestone-1',
    '1-rc-1',
    '1-rc-2',
    '1-SNAPSHOT',
    '1',
    '1-sp',
    '1-sp-2',
    '1-foo',
    '1-1',
    '1-2',
    '1.0.1',
    '1.1-alpha-1',
    '1.1-SNAPSHOT',
    '1.1',
    '1.1-20261108.120000-1',
    '1.1-20261108.120000-2',
    '1.1-20261109.010101-1',
    '1.1.1',
    '1.2',
    '1.10',
    '2.0.0-M1',
    '2.0.0-RC1',
    '2.0.0',
    '10',
    '10.0.1',
]
EQUAL = [
    ('1', '1.0'),
    ('1.0', '1.0.0'),
    ('1', '1-ga'),
    ('1', '1-final'),
    ('1', '1-release'),
    ('1-cr1', '1-rc1'),
    ('1a1', '1-alpha-1'),
    ('1b2', '1-beta-2'),
    ('1m3', '1-milestone-3'),
    ('1.0-RC1', '1.0-rc-1'),
    ('1.0-SNAPSHOT', '1-snapshot'),
]


def test_compare_orders_versions_like_maven() -> None:
    for low, high in pairwise(ASCENDING):
        assert compare(low, high) < 0, (low, high)
        assert compare(high, low) > 0, (high, low)
    for a, b in EQUAL:
        assert compare(a, b) == 0, (a, b)


def test_sort_key_follows_compare() -> None:
    keys = [sort_key(v) for v in ASCENDING]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)
    for a, b in EQUAL:
        assert sort_key(a) == sort_key(b), (a, b)
    assert sort_versions(list(reversed(ASCENDING))) == ASCENDING
    assert len(sort_key('1.' * 500)) <= 400


def test_snapshot_detection() -> None:
    assert is_snapshot('1.0-SNAPSHOT')
    assert not is_snapshot('1.0-snapshot-1')
    assert not is_snapshot('1.0')


# ---------------------------------------------------------------------------- layout
def test_parse_release_artifacts() -> None:
    a = coords.parse_artifact('org/example/lib/1.2.3/lib-1.2.3.jar')
    assert a is not None
    assert (a.group_id, a.artifact_id, a.version, a.classifier, a.extension) == (
        'org.example',
        'lib',
        '1.2.3',
        None,
        'jar',
    )
    assert not a.snapshot and a.timestamp is None
    s = coords.parse_artifact('org/example/lib/1.2.3/lib-1.2.3-sources.jar')
    assert s is not None and s.classifier == 'sources' and s.extension == 'jar'
    t = coords.parse_artifact('org/example/lib/1.2.3/lib-1.2.3-bin.tar.gz')
    assert t is not None and t.classifier == 'bin' and t.extension == 'tar.gz'
    asc = coords.parse_artifact('org/example/lib/1.2.3/lib-1.2.3.pom.asc')
    assert asc is not None and asc.signature and asc.extension == 'pom.asc'
    assert coords.parse_artifact('lib/1.0/lib-1.0.jar') is None  # no groupId
    assert coords.parse_artifact('org/example/lib/1.0/other-1.0.jar') is None
    assert coords.parse_artifact('org/example/lib/1.0/lib-1.1.jar') is None
    assert coords.parse_artifact('org/example/lib/1.0/lib-1.0') is None
    assert coords.parse_artifact('org/.hidden/lib/1.0/lib-1.0.jar') is None
    assert coords.parse_artifact('org/example/lib/1.0/maven-metadata.xml') is None


def test_parse_snapshot_artifacts() -> None:
    t = coords.parse_artifact('com/acme/app/2.0-SNAPSHOT/app-2.0-20261108.123456-7-tests.jar')
    assert t is not None
    assert (t.version, t.file_version, t.classifier, t.extension) == (
        '2.0-SNAPSHOT',
        '2.0-20261108.123456-7',
        'tests',
        'jar',
    )
    assert t.snapshot and t.timestamp == ('20261108.123456', 7)
    plain = coords.parse_artifact('com/acme/app/2.0-SNAPSHOT/app-2.0-SNAPSHOT.pom')
    assert plain is not None and plain.file_version == '2.0-SNAPSHOT' and plain.timestamp is None


def test_checksums_and_metadata_paths() -> None:
    assert coords.split_checksum('a/b/c/1/c-1.jar.sha1') == ('a/b/c/1/c-1.jar', 'sha1')
    assert coords.split_checksum('a/b/maven-metadata.xml.sha512') == ('a/b/maven-metadata.xml', 'sha512')
    assert coords.split_checksum('a/b/c.jar') is None
    assert coords.split_checksum('a/.sha1') is None
    assert coords.parse_checksum(b'0123456789abcdef0123456789abcdef01234567\n', 'sha1') == (
        '0123456789abcdef0123456789abcdef01234567'
    )
    assert coords.parse_checksum(b'0123456789ABCDEF0123456789ABCDEF01234567  lib-1.0.jar', 'sha1') == (
        '0123456789abcdef0123456789abcdef01234567'
    )
    assert coords.parse_checksum(b'<html>', 'sha1') is None
    assert coords.parse_checksum(b'\xff\xfe', 'md5') is None
    assert coords.parse_checksum(b'', 'md5') is None
    meta = coords.parse_metadata_path('org/example/lib/maven-metadata.xml')
    assert meta is not None and meta.ga == ('org.example', 'lib') and meta.snapshot_version is None
    snap = coords.parse_metadata_path('org/example/lib/1.0-SNAPSHOT/maven-metadata.xml')
    assert snap is not None and snap.snapshot_version == ('org.example', 'lib', '1.0-SNAPSHOT')
    assert coords.version_of_path('org/example/lib/1.0-SNAPSHOT/maven-metadata.xml.sha1') == '1.0-SNAPSHOT'
    assert coords.version_of_path('org/example/lib/1.0/lib-1.0.jar.md5') == '1.0'
    assert coords.version_of_path('org/example/lib/maven-metadata.xml') is None
    assert coords.strict_ok('org/example/lib/1.0/lib-1.0.jar.sha256')
    assert coords.strict_ok('org/example/maven-metadata.xml')
    assert not coords.strict_ok('maven-metadata.xml')
    assert not coords.strict_ok('index.html')
    assert coords.content_type('a/b/c-1.jar') == 'application/java-archive'
    assert coords.content_type('a/b/c-1.pom') == 'application/xml'
    assert coords.content_type('a/b/c-1.bin') == 'application/octet-stream'


def test_version_policy() -> None:
    release, snapshot, mixed = (MavenOptions(version_policy=p) for p in ('release', 'snapshot', 'mixed'))
    assert policy_allows(release, '1.0') and not policy_allows(release, '1.0-SNAPSHOT')
    assert policy_allows(snapshot, '1.0-SNAPSHOT') and not policy_allows(snapshot, '1.0')
    assert policy_allows(mixed, '1.0') and policy_allows(mixed, '1.0-SNAPSHOT')
    assert policy_allows(release, None)


# ---------------------------------------------------------------------------- maven-metadata.xml
GA_DOC = b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata xmlns="http://maven.apache.org/METADATA/1.1.0">
  <groupId>org.example</groupId>
  <artifactId>lib</artifactId>
  <versioning>
    <latest>1.1</latest>
    <release>1.1</release>
    <versions><version>1.0</version><version>1.1</version></versions>
    <lastUpdated>20261101000000</lastUpdated>
  </versioning>
</metadata>
"""


def test_parse_render_round_trip() -> None:
    doc = md.parse(GA_DOC)
    assert (doc.group_id, doc.artifact_id, doc.versions, doc.latest, doc.release) == (
        'org.example',
        'lib',
        ['1.0', '1.1'],
        '1.1',
        '1.1',
    )
    again = md.parse(md.render(doc))
    assert again == doc
    assert md.render(doc) == md.render(again)


@pytest.mark.parametrize(
    'data',
    [
        b'<metadata><groupId>x',
        b'<project/>',
        b'<?xml version="1.0"?><!DOCTYPE m [<!ENTITY a "aaaa">]><metadata>&a;</metadata>',
        b'<!DOCTYPE metadata SYSTEM "http://example.com/x.dtd"><metadata/>',
        b' ' * (md.MAX_BYTES + 1),
    ],
)
def test_parse_refuses_bad_documents(data: bytes) -> None:
    with pytest.raises(ValidationFailed):
        md.parse(data)


def test_merge_unites_versions_and_recomputes_latest_release() -> None:
    a = md.versions_metadata('org.example', 'lib', ['1.0', '1.1'], '20261101000000')
    b = md.versions_metadata('org.example', 'lib', ['1.2-SNAPSHOT', '1.0', '1.10'], '20261102000000')
    merged = md.merge([a, b])
    assert merged.versions == ['1.0', '1.1', '1.2-SNAPSHOT', '1.10']
    assert merged.latest == '1.10' and merged.release == '1.10'
    assert merged.last_updated == '20261102000000'
    c = md.versions_metadata('org.example', 'lib', ['2.0-SNAPSHOT'], '20261103000000')
    assert md.merge([a, c]).latest == '2.0-SNAPSHOT'
    assert md.merge([a, c]).release == '1.1'


def test_merge_snapshot_and_plugin_documents() -> None:
    old = md.Metadata(
        group_id='g',
        artifact_id='a',
        version='1.0-SNAPSHOT',
        snapshot_timestamp='20261101.000000',
        snapshot_build=3,
        snapshot_versions=[md.SnapshotVersion(None, 'jar', '1.0-20261101.000000-3', '20261101000000')],
    )
    new = md.Metadata(
        group_id='g',
        artifact_id='a',
        version='1.0-SNAPSHOT',
        snapshot_timestamp='20261102.000000',
        snapshot_build=1,
        snapshot_versions=[
            md.SnapshotVersion(None, 'jar', '1.0-20261102.000000-1', '20261102000000'),
            md.SnapshotVersion('sources', 'jar', '1.0-20261102.000000-1', '20261102000000'),
        ],
    )
    merged = md.merge([old, new])
    assert (merged.snapshot_timestamp, merged.snapshot_build) == ('20261102.000000', 1)
    assert [v.value for v in merged.snapshot_versions] == ['1.0-20261102.000000-1', '1.0-20261102.000000-1']
    plugins = md.merge(
        [
            md.Metadata(plugins=[md.Plugin('A', 'a', 'a-maven-plugin')]),
            md.Metadata(plugins=[md.Plugin('B', 'b', 'b-maven-plugin'), md.Plugin('A2', 'a', 'other')]),
        ]
    )
    assert [(p.prefix, p.artifact_id) for p in plugins.plugins] == [('a', 'a-maven-plugin'), ('b', 'b-maven-plugin')]
    rendered = md.parse(md.render(plugins))
    assert len(rendered.plugins) == 2 and rendered.group_id is None


def test_snapshot_metadata_from_files() -> None:
    when = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    folder = 'org/example/app/1.0-SNAPSHOT/'
    files = [
        StoredFile(folder + 'app-1.0-20261108.110000-1.jar', when),
        StoredFile(folder + 'app-1.0-20261108.110000-1.jar.sha1', when),
        StoredFile(folder + 'app-1.0-20261108.110000-1.pom', when),
        StoredFile(folder + 'app-1.0-20261108.120000-2.jar', when),
        StoredFile(folder + 'app-1.0-20261108.120000-2-sources.jar', when),
        StoredFile(folder + 'maven-metadata.xml', when),
    ]
    doc = snapshot_metadata('org.example', 'app', '1.0-SNAPSHOT', files)
    assert doc is not None
    assert (doc.snapshot_timestamp, doc.snapshot_build) == ('20261108.120000', 2)
    entries = {(v.classifier, v.extension): v.value for v in doc.snapshot_versions}
    assert entries == {
        (None, 'jar'): '1.0-20261108.120000-2',
        (None, 'pom'): '1.0-20261108.110000-1',
        ('sources', 'jar'): '1.0-20261108.120000-2',
    }
    assert md.parse(md.render(doc)).snapshot_build == 2
    assert snapshot_metadata('org.example', 'app', '1.0-SNAPSHOT', files[-1:]) is None
    plain = snapshot_metadata('org.example', 'app', '1.0-SNAPSHOT', [StoredFile(folder + 'app-1.0-SNAPSHOT.jar', when)])
    assert plain is not None and plain.local_copy and plain.snapshot_versions[0].value == '1.0-SNAPSHOT'


# ---------------------------------------------------------------------------- "Set me up"
def _ctx(repo_type: RepoType, policy: str = 'release') -> SetupContext:
    now = datetime.now(UTC)
    repo = RepoConfig.model_validate(
        {
            '_id': 'r1',
            'name': 'maven-releases',
            'format': 'maven',
            'type': repo_type,
            'format_options': {'version_policy': policy},
            'created_at': now,
            'updated_at': now,
        }
    )
    return SetupContext(
        repo=repo, base_url='https://registry.example.com', registry_host='registry.example.com', username='alice'
    )


def test_setup_snippets() -> None:
    proxy = DESCRIPTOR.setup_snippets(_ctx(RepoType.PROXY))
    maven = next(c for c in proxy if c.id == 'maven')
    settings = maven.snippets[0].content
    assert '<mirrorOf>*</mirrorOf>' in settings
    assert 'https://registry.example.com/repository/maven-releases/' in settings
    assert '{{TOKEN}}' in settings and '{{USERNAME}}' in settings
    assert not any(s.id == 'pom-distribution' for s in maven.snippets)
    hosted = DESCRIPTOR.setup_snippets(_ctx(RepoType.HOSTED, 'snapshot'))
    pom = next(s for c in hosted for s in c.snippets if s.id == 'pom-distribution')
    assert '<snapshotRepository>' in pom.content and '<repository>' not in pom.content
    gradle = next(c for c in hosted if c.id == 'gradle')
    assert any('maven-publish' in s.content for s in gradle.snippets)
    assert {c.id for c in hosted} == {'maven', 'gradle', 'sbt'}
