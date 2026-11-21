"""The ``version_sort`` contract (``waystation_common.versionsort``) and component keys (``waystation_common.
components``): every sort string orders like the format's own comparison."""

import functools
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from waystation_common.cleanup.versions import dpkg_compare, rpm_compare, sort_key
from waystation_common.components import ComponentKey, docker_key, docker_ref, key_for
from waystation_common.versionsort import (
    MAX_KEY,
    dpkg_sort_string,
    natural_sort_string,
    rpm_sort_string,
    version_sort,
)
from waystation_common.versionsort.maven import compare as maven_compare


def shuffled(items: list[str]) -> list[str]:
    return random.Random(1).sample(items, len(items))


def _sign(n: int) -> int:
    return (n > 0) - (n < 0)


def _cmp(a: str, b: str) -> int:
    return (a > b) - (a < b)


DPKG = st.text(alphabet='0123456789abz.+~-:', min_size=1, max_size=12)
RPM = st.text(alphabet='0123456789abz.~^_-', min_size=1, max_size=12)


@settings(max_examples=600, deadline=None)
@given(a=DPKG, b=DPKG)
def test_dpkg_sort_string_orders_like_dpkg(a: str, b: str) -> None:
    assert _cmp(dpkg_sort_string(a), dpkg_sort_string(b)) == _sign(dpkg_compare(a, b)), (a, b)


@settings(max_examples=600, deadline=None)
@given(a=RPM, b=RPM)
def test_rpm_sort_string_orders_like_rpm(a: str, b: str) -> None:
    assert _cmp(rpm_sort_string(a), rpm_sort_string(b)) == _sign(rpm_compare(a, b)), (a, b)


@pytest.mark.parametrize(
    'ordered',
    [
        ['1.0~rc1', '1.0', '1.0a', '1.0.1', '1:0.1'],
        ['2.36-9+deb12u7', '2.36-9+deb12u10', '2.37-1'],
        ['7.88.1-10+deb12u5', '8.14.1-2'],
    ],
)
def test_dpkg_examples(ordered: list[str]) -> None:
    assert sorted(ordered, key=dpkg_sort_string) == ordered


@settings(max_examples=300, deadline=None)
@given(a=st.from_regex(r'[0-9a-z.\-]{1,10}', fullmatch=True), b=st.from_regex(r'[0-9a-z.\-]{1,10}', fullmatch=True))
def test_natural_sort_string_orders_like_the_natural_key(a: str, b: str) -> None:
    natural = sort_key('raw', a), sort_key('raw', b)
    expected = (natural[0] > natural[1]) - (natural[0] < natural[1])
    assert _cmp(natural_sort_string(a), natural_sort_string(b)) == expected, (a, b)


def test_version_sort_per_format() -> None:
    npm = ['1.0.0-alpha', '1.0.0-rc.1', '1.0.0', '1.2.0', '10.0.0']
    assert sorted(shuffled(npm), key=lambda v: version_sort('npm', v)) == npm
    go = ['v0.0.0-20191109021931-daa7c04131f5', 'v0.1.0', 'v1.10.0']
    assert sorted(shuffled(go), key=lambda v: version_sort('go', v)) == go
    pypi = ['1.0.dev1', '1.0a1', '1.0', '1.0.post1', '2.0']
    assert sorted(shuffled(pypi), key=lambda v: version_sort('pypi', v)) == pypi
    maven = ['1.0-alpha-1', '1.0-SNAPSHOT', '1.0', '1.0.1', '1.10']
    assert sorted(shuffled(maven), key=functools.cmp_to_key(maven_compare)) == maven
    assert sorted(shuffled(maven), key=lambda v: version_sort('maven', v)) == maven
    yum = ['1.0-1.el9', '1.0-2.el9', '1:0.9-1']
    assert sorted(shuffled(yum), key=lambda v: version_sort('yum', v)) == yum
    tags = ['1.9', '1.27', '1.27.1', 'alpine', 'latest']
    assert sorted(shuffled(tags), key=lambda v: version_sort('docker', v)) == tags
    assert version_sort('raw', '') == '' and version_sort('apt', None) == ''
    assert len(version_sort('docker', '9' * 500 + 'x' * 500)) <= MAX_KEY


@pytest.mark.parametrize(
    ('fmt', 'path', 'key'),
    [
        ('npm', 'lodash/-/lodash-4.17.21.tgz', ComponentKey('npm', '', 'lodash', '4.17.21')),
        ('npm', '@types/node/-/node-22.1.0.tgz', ComponentKey('npm', '@types', '@types/node', '22.1.0')),
        ('npm', 'lodash/-/packument.json', None),
        ('apt', 'pool/main/c/curl/curl_8.14.1-2_amd64.deb', ComponentKey('apt', '', 'curl', '8.14.1-2')),
        ('apt', 'dists/bookworm/main/binary-amd64/Packages.xz', None),
        ('yum', 'Packages/b/bash-5.2.26-4.el10.x86_64.rpm', ComponentKey('yum', '', 'bash', '5.2.26-4.el10')),
        ('yum', 'repodata/repomd.xml', None),
        ('maven', 'org/example/lib/1.0/lib-1.0.jar', ComponentKey('maven', 'org.example', 'lib', '1.0')),
        ('maven', 'org/example/lib/1.0/lib-1.0.jar.sha1', None),
        ('maven', 'org/example/lib/maven-metadata.xml', None),
        (
            'maven',
            'org/example/lib/1.0-SNAPSHOT/lib-1.0-20261108.123456-3.jar',
            ComponentKey('maven', 'org.example', 'lib', '1.0-SNAPSHOT'),
        ),
        ('pypi', 'packages/requests/requests-2.32.3-py3-none-any.whl', ComponentKey('pypi', '', 'requests', '2.32.3')),
        ('pypi', 'packages/requests/requests-2.32.3-py3-none-any.whl.metadata', None),
        ('go', 'golang.org/x/mod/@v/v0.20.0.zip', ComponentKey('go', '', 'golang.org/x/mod', 'v0.20.0')),
        ('go', 'golang.org/x/mod/@v/v0.20.0.mod', None),
        ('helm', 'charts/nginx-18.1.0.tgz', ComponentKey('helm', '', 'nginx', '18.1.0')),
        ('raw', 'tools/v1/tool.tar.gz', ComponentKey('raw', 'tools/v1', 'tool.tar.gz', '')),
        ('docker', 'library/nginx/manifests/1.27', None),
    ],
)
def test_component_keys(fmt: str, path: str, key: ComponentKey | None) -> None:
    assert key_for(fmt, path) == key


def test_component_keys_skip_metadata_and_display() -> None:
    assert key_for('raw', 'index.html', content=False) is None
    assert key_for('helm', 'index.yaml', content=False) is None
    displays = {
        ComponentKey('npm', '', 'lodash', '4.17.21'): 'lodash@4.17.21',
        docker_key('library/nginx', '1.27'): 'library/nginx:1.27',
        ComponentKey('maven', 'org.example', 'lib', '1.0'): 'org.example:lib:1.0',
        ComponentKey('pypi', '', 'requests', '2.32.3'): 'requests==2.32.3',
        ComponentKey('yum', '', 'bash', '5.2-1'): 'bash-5.2-1',
        ComponentKey('apt', '', 'curl', '8.14.1-2'): 'curl 8.14.1-2',
        ComponentKey('raw', 'tools', 'a.txt', ''): 'tools/a.txt',
        ComponentKey('raw', '', 'a.txt', ''): 'a.txt',
        ComponentKey('terraform', '', 'registry.example.com/h/aws', '5.0.0'): 'registry.example.com/h/aws 5.0.0',
    }
    for key, display in displays.items():
        assert key.display == display
    assert docker_key('a', 'b').version_sort == natural_sort_string('b')


def test_docker_refs() -> None:
    digest = 'sha256:' + 'a' * 64
    assert docker_ref('library/nginx/manifests/1.27') == ('library/nginx', '1.27', False)
    assert docker_ref('library/nginx/manifests/1.27@accept=v2') == ('library/nginx', '1.27', False)
    assert docker_ref(f'library/nginx/manifests/{digest}') == ('library/nginx', digest, True)
    assert docker_ref('blobs/' + digest) is None
    assert docker_ref('manifests/x') is None
