"""Prepare the perf stack (tests/perf/README.md): create the proxy repositories through the API, warm the cache with
every file the cache-hit scenarios read, and write ``targets.json`` for the k6 script.

Runs in the dev image on the perf stack's network (stdlib only). Signs in as the per-run bootstrap admin
(``PERF_ADMIN_PASSWORD``). Exits non-zero when a repository cannot be created or a warm-up request fails, so a
broken stack never produces numbers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ORIGIN = 'http://perf-origin:8080'
NPM_ABBREVIATED = 'application/vnd.npm.install-v1+json'
PYPI_JSON = 'application/vnd.pypi.simple.v1+json'
DOCKER_ACCEPT = 'application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'
BEARER = re.compile(r'(\w+)="([^"]*)"')
CATALOG_TTL_S = 6.0  # waystation_server.repos.catalog refreshes every 5 s
KEEP_BYTES = 4 * 1024 * 1024


class SetupError(Exception):
    pass


def _read_bounded(resp: Any) -> bytes:
    """The body's first KEEP_BYTES (API answers, errors); the rest of a large file is read and dropped."""
    kept = resp.read(KEEP_BYTES)
    while resp.read(1024 * 1024):
        pass
    return bytes(kept)


@dataclass
class Client:
    base: str
    cookie: str = ''
    csrf: str = ''
    tokens: dict[str, str] = field(default_factory=dict)

    def request(
        self, method: str, url: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url if url.startswith('http') else self.base + url, data=data, method=method)
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        if self.cookie and url.startswith('/api/'):
            req.add_header('Cookie', self.cookie)
            req.add_header('X-CSRF-Token', self.csrf)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, _read_bounded(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()

    def wait(self, timeout_s: float = 180) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if self.request('GET', '/healthz')[0] == 200:
                    return
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise SetupError('the stack did not become healthy')
            time.sleep(1)

    def login(self, password: str) -> None:
        req = urllib.request.Request(
            self.base + '/api/v1/auth/login',
            data=json.dumps({'username': 'admin', 'password': password}).encode(),
            method='POST',
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            session = json.loads(resp.read())
            cookies = [c.split(';', 1)[0] for c in resp.headers.get_all('Set-Cookie') or []]
        self.cookie = '; '.join(cookies)
        self.csrf = session['csrf_token']

    def ensure_repo(self, spec: dict[str, Any]) -> dict[str, Any]:
        status, _, body = self.request('GET', f'/api/v1/repositories/{spec["name"]}')
        if status == 200:
            return dict(json.loads(body))
        status, _, body = self.request('POST', '/api/v1/repositories', spec)
        if status not in (200, 201):
            raise SetupError(f'creating {spec["name"]}: {status} {body[:500]!r}')
        return dict(json.loads(body))

    def docker_token(self, url: str) -> str:
        """A pull token for the image of ``url``, from the registry's own 401 challenge (anonymous)."""
        for _ in range(40):  # 404 until every server process has seen the new repository (see warm)
            status, headers, _ = self.request('GET', url)
            if status != 404:
                break
            time.sleep(0.25)
        if status != 401:
            return ''
        params = dict(BEARER.findall(headers.get('www-authenticate', '')))
        query = urllib.parse.urlencode({k: v for k, v in params.items() if k in ('service', 'scope')})
        realm = params['realm']
        realm = realm if realm.startswith('http') else self.base + realm
        status, _, body = self.request('GET', f'{realm}?{query}')
        if status != 200:
            raise SetupError(f'docker token: {status} {body[:300]!r}')
        token = json.loads(body)
        return str(token.get('token') or token.get('access_token'))


def proxy(name: str, fmt: str, upstream: str, **extra: Any) -> dict[str, Any]:
    return {
        'name': name,
        'format': fmt,
        'type': 'proxy',
        'online': True,
        'description': 'Perf suite (synthetic upstream)',
        'proxy': {'upstream_url': upstream, 'allow_private_upstream': True, 'metadata_max_age_s': 86_400},
        **extra,
    }


REPOS = [
    proxy('raw-perf', 'raw', f'{ORIGIN}/raw/'),
    proxy('npm-perf', 'npm', f'{ORIGIN}/npm/'),
    proxy('pypi-perf', 'pypi', f'{ORIGIN}/pypi'),
    proxy('maven-perf', 'maven', f'{ORIGIN}/maven/'),
    proxy('apt-perf', 'apt', f'{ORIGIN}/apt/', format_options={'distribution': 'perf'}),
    proxy('docker-perf', 'docker', ORIGIN),
    proxy('perf-bulk', 'raw', f'{ORIGIN}/bulk/'),  # API list/search: seeded by seed_assets.py, never fetched
]


def targets(catalog: dict[str, Any]) -> dict[str, Any]:
    """The requests of each scenario: ``[url, accept]`` pairs relative to the stack's base URL."""
    raw = catalog['raw']
    docker = [
        [f'/v2/docker-perf/{img["image"]}/manifests/{ref}', DOCKER_ACCEPT]
        for img in catalog['docker']['images']
        for ref in (img['tag'], img['digest'])
    ]
    return {
        'hit_small': [f'/repository/raw-perf/{p}' for p in raw['small']],
        'hit_medium': [f'/repository/raw-perf/{p}' for p in raw['medium']],
        'hit_large': f'/repository/raw-perf/{raw["large"]}',
        'large_bytes': raw['large_bytes'],
        'metadata': [[f'/repository/npm-perf/{n}', NPM_ABBREVIATED] for n in catalog['npm']['packages']]
        + [[f'/repository/npm-perf/{n}', 'application/json'] for n in catalog['npm']['packages']]
        + [[f'/repository/pypi-perf/simple/{n}/', PYPI_JSON] for n in catalog['pypi']['projects']]
        + [[f'/repository/pypi-perf/simple/{n}/', 'text/html'] for n in catalog['pypi']['projects']]
        + [[f'/repository/maven-perf/{p}', '*/*'] for p in catalog['maven']['metadata']]
        + [[f'/repository/apt-perf/{catalog["apt"]["release"]}', '*/*']]
        + [[f'/repository/apt-perf/{catalog["apt"]["packages"]}', '*/*']],
        'docker': docker,
        'miss_small': [f'/repository/raw-perf/{p}' for p in raw['miss_small']],
        'miss_small_direct': [f'{ORIGIN}/raw/{p}' for p in raw['miss_small']],
        'miss_large': [f'/repository/raw-perf/{p}' for p in raw['miss_large']],
        'miss_large_direct': [f'{ORIGIN}/raw/{p}' for p in raw['miss_large']],
        'miss_large_bytes': raw['miss_large_bytes'],
        'slow': [f'/repository/raw-perf/{p}' for p in raw['slow']],
        'npm_tarballs': [f'/repository/npm-perf/{t}' for t in catalog['npm']['tarballs']],
    }


def warm(client: Client, t: dict[str, Any]) -> int:
    """Fetch every file a cache-hit scenario reads once (fills the cache); docker manifests need a pull token."""
    jobs: list[tuple[str, dict[str, str]]] = [(u, {}) for u in t['hit_small'] + t['hit_medium'] + [t['hit_large']]]
    jobs += [(u, {'Accept': accept}) for u, accept in t['metadata']]
    jobs += [(u, {}) for u in t['npm_tarballs']]
    for url, accept in t['docker']:
        token = client.docker_token(url)
        jobs.append((url, {'Accept': accept} | ({'Authorization': f'Bearer {token}'} if token else {})))

    def fetch(job: tuple[str, dict[str, str]]) -> str | None:
        # A new repository reaches every server process's catalog within moments (change stream): until then a
        # process that has not seen it answers 404.
        for _ in range(40):
            status, _, body = client.request('GET', job[0], headers=job[1])
            if status != 404:
                break
            time.sleep(0.25)
        return None if status == 200 else f'{job[0]}: {status} {body[:200]!r}'

    with ThreadPoolExecutor(8) as pool:
        errors = [e for e in pool.map(fetch, jobs) if e]
    if errors:
        raise SetupError('warm-up failed:\n  ' + '\n  '.join(errors[:20]))
    return len(jobs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('--base', default='http://waystation-web:8080')
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    client = Client(args.base)
    try:
        client.wait()
        client.login(os.environ['PERF_ADMIN_PASSWORD'])
        for spec in REPOS:
            client.ensure_repo(spec)
        # Every server process refreshes its repository catalog within CATALOG_TTL_S. Until then a process that has
        # not seen a new repository answers 404, and its token service grants nothing on it (an anonymous docker pull
        # token issued in that window is refused later), so wait it out once.
        time.sleep(CATALOG_TTL_S)
        t = targets(json.loads(args.catalog.read_text()))
        started = time.monotonic()
        count = warm(client, t)
        print(f'setup: {len(REPOS)} repositories, {count} files warmed in {time.monotonic() - started:.1f} s')
    except SetupError as exc:
        print(f'setup: {exc}', file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(t, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
