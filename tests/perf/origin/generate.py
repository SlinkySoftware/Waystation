"""Synthetic upstream content for the perf suite (tests/perf/README.md).

Writes, under ``--out``:

- ``docroot/``: what the origin (nginx) serves: raw files (hits, misses, slow files for the live-event probe), npm
  packuments and tarballs, PyPI simple pages and wheels, ``maven-metadata.xml`` files, an apt suite (``Release``,
  ``Packages``, ``Packages.gz``) and Docker/OCI images (manifests, configs, layers);
- ``origin.conf``: the origin's nginx site, with one exact location per Docker manifest/blob (content type and
  ``Docker-Content-Digest`` headers) and the rate limits of the miss and slow paths;
- ``catalog.json``: what was generated (names, paths, sizes), read by ``setup.py`` and the k6 script.

Everything is synthetic and deterministic for a given profile. Large files are sparse (zeros, no disk used on the
origin side). Stdlib only; runs in the dev image.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ORIGIN = 'http://perf-origin:8080'
KIB = 1024
MIB = 1024 * KIB


@dataclass(frozen=True)
class Profile:
    small_files: int  # cache-hit content, 4 KiB each
    medium_files: int  # cache-hit content for the concurrency scenario, 1 MiB each
    large_mib: int  # one large cached file (TTFB, ranges, line rate)
    miss_small: int  # unique small files fetched once each through Waystation (added TTFB)
    miss_large: int  # unique large files fetched once each through Waystation (throughput)
    miss_large_mib: int
    slow_files: int  # rate-limited files for the live-event probe
    slow_mib: int
    origin_rate: str  # nginx limit_rate for the miss paths ('0' = unlimited)
    packages: int  # npm packages, PyPI projects, Maven artifacts, Docker images
    versions: int  # versions per npm/PyPI/Maven package
    apt_stanzas: int  # stanzas in the apt Packages index


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as f:
        f.truncate(size)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw_files(docroot: Path, p: Profile, rng: random.Random) -> dict[str, Any]:
    base = docroot / 'raw'
    small = [f'hit/small-{i:04d}.bin' for i in range(p.small_files)]
    for name in small:
        _write(base / name, rng.randbytes(4 * KIB))
    medium = [f'hit/medium-{i:04d}.bin' for i in range(p.medium_files)]
    for name in medium:
        _write(base / name, rng.randbytes(MIB))
    large = 'hit/large.bin'
    _sparse(base / large, p.large_mib * MIB)
    miss_small = [f'miss/small-{i:05d}.bin' for i in range(p.miss_small)]
    for name in miss_small:
        _write(base / name, rng.randbytes(4 * KIB))
    miss_large = [f'miss/large-{i:03d}.bin' for i in range(p.miss_large)]
    for name in miss_large:
        _sparse(base / name, p.miss_large_mib * MIB)
    slow = [f'slow/live-{i:03d}.bin' for i in range(p.slow_files)]
    for name in slow:
        _sparse(base / name, p.slow_mib * MIB)
    return {
        'small': small,
        'medium': medium,
        'large': large,
        'large_bytes': p.large_mib * MIB,
        'miss_small': miss_small,
        'miss_large': miss_large,
        'miss_large_bytes': p.miss_large_mib * MIB,
        'slow': slow,
        'slow_bytes': p.slow_mib * MIB,
    }


def _version(i: int) -> str:
    return f'1.{i // 10}.{i % 10}'


def npm_packages(docroot: Path, p: Profile, rng: random.Random) -> dict[str, Any]:
    names = [f'perf-pkg-{i:04d}' for i in range(p.packages)]
    tarballs = []
    for name in names:
        latest = _version(p.versions - 1)
        tgz = rng.randbytes(16 * KIB)
        file = f'{name}-{latest}.tgz'
        _write(docroot / 'npm' / name / '-' / file, tgz)
        tarballs.append(f'{name}/-/{file}')
        versions = {}
        for v in range(p.versions):
            ver = _version(v)
            data = tgz if ver == latest else rng.randbytes(64)  # only the latest tarball exists on the origin
            versions[ver] = {
                'name': name,
                'version': ver,
                'description': f'Synthetic package {name} for the Waystation perf suite',
                'license': 'MIT',
                'dependencies': {names[(names.index(name) + k) % len(names)]: '^1.0.0' for k in (1, 2)},
                'dist': {
                    'tarball': f'{ORIGIN}/npm/{name}/-/{name}-{ver}.tgz',
                    'shasum': hashlib.sha1(data, usedforsecurity=False).hexdigest(),
                    'integrity': 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode(),
                },
            }
        packument = {
            'name': name,
            'description': f'Synthetic package {name}',
            'dist-tags': {'latest': latest},
            'versions': versions,
            'time': {'modified': '2026-11-10T00:00:00.000Z', **dict.fromkeys(versions, '2026-11-01T00:00:00.000Z')},
            'license': 'MIT',
        }
        _write(docroot / 'npm' / '_packuments' / f'{name}.json', json.dumps(packument, indent=1).encode())
    return {'packages': names, 'tarballs': tarballs}


def pypi_projects(docroot: Path, p: Profile, rng: random.Random) -> dict[str, Any]:
    names = [f'perf-proj-{i:04d}' for i in range(p.packages)]
    for name in names:
        dist = name.replace('-', '_')
        links = []
        for v in range(p.versions):
            ver = _version(v)
            wheel = f'{dist}-{ver}-py3-none-any.whl'
            data = rng.randbytes(8 * KIB if v == p.versions - 1 else 64)
            _write(docroot / 'pypi' / 'files' / name / wheel, data)
            links.append(
                f'<a href="{ORIGIN}/pypi/files/{name}/{wheel}#sha256={_sha256(data)}" '
                f'data-requires-python="&gt;=3.9">{wheel}</a><br>'
            )
        page = (
            '<!DOCTYPE html>\n<html><head><meta name="pypi:repository-version" content="1.0">'
            f'<title>Links for {name}</title></head><body><h1>Links for {name}</h1>\n'
            + '\n'.join(links)
            + '\n</body></html>\n'
        )
        _write(docroot / 'pypi' / 'simple' / name / 'index.html', page.encode())
    return {'projects': names}


def maven_artifacts(docroot: Path, p: Profile) -> dict[str, Any]:
    group = 'com/example/perf'
    paths = []
    for i in range(p.packages):
        artifact = f'lib-{i:04d}'
        versions = ''.join(f'<version>{_version(v)}</version>' for v in range(p.versions))
        latest = _version(p.versions - 1)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<metadata>\n  <groupId>com.example.perf</groupId>\n'
            f'  <artifactId>{artifact}</artifactId>\n  <versioning>\n    <latest>{latest}</latest>\n'
            f'    <release>{latest}</release>\n    <versions>{versions}</versions>\n'
            '    <lastUpdated>20261110000000</lastUpdated>\n  </versioning>\n</metadata>\n'
        )
        path = f'{group}/{artifact}/maven-metadata.xml'
        _write(docroot / 'maven' / path, xml.encode())
        paths.append(path)
    return {'metadata': paths}


def apt_suite(docroot: Path, p: Profile) -> dict[str, Any]:
    stanzas = []
    for i in range(p.apt_stanzas):
        name = f'perf-deb-{i:05d}'
        digest = hashlib.sha256(name.encode()).hexdigest()
        stanzas.append(
            f'Package: {name}\nVersion: 1.0.{i % 100}-1\nArchitecture: amd64\nMaintainer: Perf <perf@example.com>\n'
            f'Installed-Size: 64\nFilename: pool/main/p/{name}/{name}_1.0.{i % 100}-1_amd64.deb\nSize: 4096\n'
            f'SHA256: {digest}\nSection: misc\nPriority: optional\nDescription: synthetic package {i}\n'
        )
    packages = '\n'.join(stanzas).encode()
    packages_gz = gzip.compress(packages, mtime=0)
    base = docroot / 'apt' / 'dists' / 'perf'
    _write(base / 'main' / 'binary-amd64' / 'Packages', packages)
    _write(base / 'main' / 'binary-amd64' / 'Packages.gz', packages_gz)
    release = (
        'Origin: Perf\nLabel: Perf\nSuite: perf\nCodename: perf\nDate: Tue, 10 Nov 2026 00:00:00 UTC\n'
        'Architectures: amd64\nComponents: main\nDescription: Waystation perf suite\nSHA256:\n'
        f' {_sha256(packages)} {len(packages)} main/binary-amd64/Packages\n'
        f' {_sha256(packages_gz)} {len(packages_gz)} main/binary-amd64/Packages.gz\n'
    )
    _write(base / 'Release', release.encode())
    return {'release': 'dists/perf/Release', 'packages': 'dists/perf/main/binary-amd64/Packages.gz'}


def yum_repository(docroot: Path, p: Profile) -> dict[str, Any]:
    """One yum repository directory: ``repodata/repomd.xml`` listing a by-checksum ``primary.xml.gz``."""
    packages = []
    for i in range(p.apt_stanzas):
        name = f'perf-rpm-{i:05d}'
        digest = hashlib.sha256(name.encode()).hexdigest()
        packages.append(
            f'<package type="rpm"><name>{name}</name><arch>x86_64</arch>'
            f'<version epoch="0" ver="1.0.{i % 100}" rel="1"/><checksum type="sha256" pkgid="YES">{digest}</checksum>'
            f'<summary>synthetic package {i}</summary><size package="4096" installed="8192" archive="8192"/>'
            f'<location href="Packages/p/{name}-1.0.{i % 100}-1.x86_64.rpm"/></package>'
        )
    primary = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<metadata xmlns="http://linux.duke.edu/metadata/common" '
        f'xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="{len(packages)}">\n'
        + '\n'.join(packages)
        + '\n</metadata>\n'
    ).encode()
    primary_gz = gzip.compress(primary, mtime=0)
    href = f'repodata/{_sha256(primary_gz)}-primary.xml.gz'
    repomd = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<repomd xmlns="http://linux.duke.edu/metadata/repo" '
        'xmlns:rpm="http://linux.duke.edu/metadata/rpm">\n  <revision>1762732800</revision>\n'
        f'  <data type="primary">\n    <checksum type="sha256">{_sha256(primary_gz)}</checksum>\n'
        f'    <open-checksum type="sha256">{_sha256(primary)}</open-checksum>\n    <location href="{href}"/>\n'
        f'    <timestamp>1762732800</timestamp>\n    <size>{len(primary_gz)}</size>\n'
        f'    <open-size>{len(primary)}</open-size>\n  </data>\n</repomd>\n'
    ).encode()
    base = docroot / 'yum' / 'perf' / 'x86_64'
    _write(base / href, primary_gz)
    _write(base / 'repodata' / 'repomd.xml', repomd)
    return {'repomd': 'perf/x86_64/repodata/repomd.xml', 'primary': f'perf/x86_64/{href}'}


def go_modules(docroot: Path, p: Profile) -> dict[str, Any]:
    """Version lists and ``@latest`` of ``example.com/perf/mod-<i>`` (GOPROXY protocol)."""
    modules = [f'example.com/perf/mod-{i:04d}' for i in range(p.packages)]
    for module in modules:
        versions = [f'v{_version(v)}' for v in range(p.versions)]
        _write(docroot / 'go' / module / '@v' / 'list', ('\n'.join(versions) + '\n').encode())
        latest = {'Version': versions[-1], 'Time': '2026-11-01T00:00:00Z'}
        _write(docroot / 'go' / module / '@latest', json.dumps(latest).encode())
    return {'modules': modules}


MANIFEST_TYPE = 'application/vnd.oci.image.manifest.v1+json'


def docker_images(docroot: Path, p: Profile, rng: random.Random) -> tuple[dict[str, Any], list[str]]:
    """OCI images ``perf/app-<i>:latest`` (config + 2 layers). Returns the catalog and nginx locations."""
    store = docroot / 'docker'
    locations = []
    images = []

    def blob(data: bytes) -> str:
        digest = 'sha256:' + _sha256(data)
        _write(store / 'blobs' / digest.removeprefix('sha256:'), data)
        return digest

    for i in range(max(1, p.packages // 4)):
        image = f'perf/app-{i:03d}'
        layers = [rng.randbytes(32 * KIB) for _ in range(2)]
        layer_digests = [blob(data) for data in layers]
        config = json.dumps(
            {
                'architecture': 'amd64',
                'os': 'linux',
                'config': {'Cmd': ['/bin/true']},
                'rootfs': {'type': 'layers', 'diff_ids': layer_digests},
            }
        ).encode()
        config_digest = blob(config)
        manifest = json.dumps(
            {
                'schemaVersion': 2,
                'mediaType': MANIFEST_TYPE,
                'config': {
                    'mediaType': 'application/vnd.oci.image.config.v1+json',
                    'digest': config_digest,
                    'size': len(config),
                },
                'layers': [
                    {'mediaType': 'application/vnd.oci.image.layer.v1.tar+gzip', 'digest': d, 'size': len(data)}
                    for d, data in zip(layer_digests, layers, strict=True)
                ],
            }
        ).encode()
        manifest_digest = 'sha256:' + _sha256(manifest)
        _write(store / 'manifests' / manifest_digest.removeprefix('sha256:'), manifest)
        file = f'/srv/origin/docker/manifests/{manifest_digest.removeprefix("sha256:")}'
        for ref in ('latest', manifest_digest):
            locations.append(
                f'    location = /v2/{image}/manifests/{ref} {{\n'
                f'        types {{ }}\n        default_type {MANIFEST_TYPE};\n'
                f'        add_header Docker-Content-Digest {manifest_digest} always;\n'
                f'        add_header Docker-Distribution-API-Version registry/2.0 always;\n'
                f'        alias {file};\n    }}\n'
            )
        images.append({'image': image, 'tag': 'latest', 'digest': manifest_digest, 'blobs': layer_digests})
    return {'images': images}, locations


ORIGIN_CONF = """# Generated by tests/perf/origin/generate.py; the perf suite's synthetic upstream.
server {{
    listen 8080;
    server_name _;
    root /srv/origin;
    sendfile on;
    tcp_nopush on;
    etag on;
    access_log off;

    location /raw/miss/ {{
        limit_rate {rate};
        default_type application/octet-stream;
    }}
    location /raw/slow/ {{
        limit_rate {slow_rate};
        default_type application/octet-stream;
    }}
    location /raw/ {{
        default_type application/octet-stream;
    }}
    location ~ ^/npm/([^/_][^/]*)$ {{
        types {{ }}
        default_type application/json;
        try_files /npm/_packuments/$1.json =404;
    }}
    location /npm/ {{
        default_type application/octet-stream;
    }}
    location /pypi/simple/ {{
        types {{ }}
        default_type "text/html; charset=utf-8";
        index index.html;
    }}
    location /pypi/files/ {{
        default_type application/octet-stream;
    }}
    location /maven/ {{
        types {{ xml text/xml; }}
        default_type application/octet-stream;
    }}
    location /apt/ {{
        types {{ }}
        default_type application/octet-stream;
    }}
    location /yum/ {{
        types {{ xml text/xml; }}
        default_type application/octet-stream;
    }}
    location /go/ {{
        types {{ }}
        default_type "text/plain; charset=utf-8";
    }}
    location ~ /@latest$ {{
        types {{ }}
        default_type application/json;
    }}
    location = /v2/ {{
        types {{ }}
        default_type application/json;
        add_header Docker-Distribution-API-Version registry/2.0 always;
        return 200 '{{}}';
    }}
    location ~ "^/v2/(.+)/blobs/sha256:([0-9a-f]{{64}})$" {{
        types {{ }}
        default_type application/octet-stream;
        add_header Docker-Content-Digest sha256:$2 always;
        try_files /docker/blobs/$2 =404;
    }}
{locations}}}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('--out', type=Path, required=True)
    for field in Profile.__dataclass_fields__:
        parser.add_argument(f'--{field.replace("_", "-")}', required=True)
    args = parser.parse_args(argv)
    values: dict[str, Any] = {
        name: (getattr(args, name) if name == 'origin_rate' else int(getattr(args, name)))
        for name in Profile.__dataclass_fields__
    }
    profile = Profile(**values)
    docroot = args.out / 'docroot'
    rng = random.Random(20261110)
    catalog: dict[str, Any] = {
        'origin': ORIGIN,
        'raw': raw_files(docroot, profile, rng),
        'npm': npm_packages(docroot, profile, rng),
        'pypi': pypi_projects(docroot, profile, rng),
        'maven': maven_artifacts(docroot, profile),
        'apt': apt_suite(docroot, profile),
        'yum': yum_repository(docroot, profile),
        'go': go_modules(docroot, profile),
    }
    catalog['docker'], locations = docker_images(docroot, profile, rng)
    rate = profile.origin_rate
    slow_rate = f'{max(1, profile.slow_mib * MIB // 4 // MIB)}m'  # a slow file takes about 4 s
    conf = ORIGIN_CONF.format(rate=rate, slow_rate=slow_rate, locations=''.join(locations))
    (args.out / 'origin.conf').write_text(conf)
    (args.out / 'catalog.json').write_text(json.dumps(catalog, indent=1))
    for path in [docroot, *docroot.rglob('*')]:  # the origin runs as nginx's unprivileged user
        path.chmod(0o755 if path.is_dir() else 0o644)
    print(f'generated {sum(1 for f in docroot.rglob("*") if f.is_file())} files in {docroot}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
