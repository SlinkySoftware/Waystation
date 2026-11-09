"""The synthetic upstream content must be what each format's proxy verifies (tests/perf/origin/generate.py)."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
from pathlib import Path

import generate

ARGS = {
    'small-files': 2,
    'medium-files': 1,
    'large-mib': 2,
    'miss-small': 3,
    'miss-large': 1,
    'miss-large-mib': 1,
    'slow-files': 1,
    'slow-mib': 8,
    'origin-rate': '100m',
    'packages': 4,
    'versions': 3,
    'apt-stanzas': 10,
}


def run(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    argv = ['--out', str(tmp_path)]
    for key, value in ARGS.items():
        argv += [f'--{key}', str(value)]
    assert generate.main(argv) == 0
    return tmp_path / 'docroot', json.loads((tmp_path / 'catalog.json').read_text())


def test_content_matches_its_metadata(tmp_path: Path) -> None:
    docroot, catalog = run(tmp_path)
    raw = catalog['raw']
    assert isinstance(raw, dict)
    assert (docroot / 'raw' / raw['large']).stat().st_size == 2 * 1024 * 1024
    assert len(raw['miss_small']) == 3

    # npm: the latest tarball matches the packument's sha1 and SRI
    packument = json.loads((docroot / 'npm' / '_packuments' / 'perf-pkg-0000.json').read_text())
    latest = packument['dist-tags']['latest']
    dist = packument['versions'][latest]['dist']
    tgz = (docroot / 'npm' / 'perf-pkg-0000' / '-' / f'perf-pkg-0000-{latest}.tgz').read_bytes()
    assert dist['shasum'] == hashlib.sha1(tgz, usedforsecurity=False).hexdigest()
    assert dist['integrity'] == 'sha512-' + base64.b64encode(hashlib.sha512(tgz).digest()).decode()

    # PyPI: every link's sha256 fragment matches its file
    page = (docroot / 'pypi' / 'simple' / 'perf-proj-0000' / 'index.html').read_text()
    links = re.findall(r'href="[^"]*/pypi/files/([^"#]+)#sha256=([0-9a-f]{64})"', page)
    assert len(links) == 3
    for path, digest in links:
        assert hashlib.sha256((docroot / 'pypi' / 'files' / path).read_bytes()).hexdigest() == digest

    # apt: Release lists the right hashes and sizes of both index files
    release = (docroot / 'apt' / 'dists' / 'perf' / 'Release').read_text()
    for name in ('Packages', 'Packages.gz'):
        data = (docroot / 'apt' / 'dists' / 'perf' / 'main' / 'binary-amd64' / name).read_bytes()
        assert f' {hashlib.sha256(data).hexdigest()} {len(data)} main/binary-amd64/{name}\n' in release
    packages = gzip.decompress(
        (docroot / 'apt' / 'dists' / 'perf' / 'main' / 'binary-amd64' / 'Packages.gz').read_bytes()
    )
    assert packages.count(b'Package: ') == 10

    # yum: repomd.xml names the primary by its checksum and lists its sizes and both checksums
    yum = catalog['yum']
    assert isinstance(yum, dict)
    repomd = (docroot / 'yum' / yum['repomd']).read_text()
    primary_gz = (docroot / 'yum' / yum['primary']).read_bytes()
    primary = gzip.decompress(primary_gz)
    assert f'<checksum type="sha256">{hashlib.sha256(primary_gz).hexdigest()}</checksum>' in repomd
    assert f'<open-checksum type="sha256">{hashlib.sha256(primary).hexdigest()}</open-checksum>' in repomd
    assert f'<size>{len(primary_gz)}</size>' in repomd
    assert primary.count(b'<package type="rpm">') == 10

    # Helm: one entry per chart version, each with a 64-hex digest and a chart URL
    index = (docroot / 'helm' / 'index.yaml').read_text()
    assert index.count('  - apiVersion: v2\n') == 4 * 3
    assert len(re.findall(r'^    digest: [0-9a-f]{64}$', index, re.MULTILINE)) == 4 * 3

    # Go: version lists end with the version @latest names
    lines = (docroot / 'go' / 'example.com/perf/mod-0000' / '@v' / 'list').read_text().split()
    assert json.loads((docroot / 'go' / 'example.com/perf/mod-0000' / '@latest').read_text())['Version'] == lines[-1]


def test_docker_manifests_and_origin_conf(tmp_path: Path) -> None:
    docroot, catalog = run(tmp_path)
    images = catalog['docker']
    assert isinstance(images, dict)
    conf = (tmp_path / 'origin.conf').read_text()
    for image in images['images']:
        digest = image['digest']
        manifest = (docroot / 'docker' / 'manifests' / digest.removeprefix('sha256:')).read_bytes()
        assert 'sha256:' + hashlib.sha256(manifest).hexdigest() == digest
        for blob in [json.loads(manifest)['config']['digest'], *image['blobs']]:
            data = (docroot / 'docker' / 'blobs' / blob.removeprefix('sha256:')).read_bytes()
            assert 'sha256:' + hashlib.sha256(data).hexdigest() == blob
        assert f'location = /v2/{image["image"]}/manifests/latest {{' in conf
        assert f'add_header Docker-Content-Digest {digest} always;' in conf
    assert 'limit_rate 100m;' in conf
    assert 'limit_rate 2m;' in conf  # an 8 MiB slow file takes about 4 s
