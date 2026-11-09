#!/usr/bin/env python3
"""Side-by-side validation of a Nexus Repository and a Waystation before cutover (T-M02;
Documents/Operations/MIGRATING_FROM_NEXUS.md).

Sends the same requests (a TOML list of checks: docker manifests, npm packuments and tarballs, apt and yum indexes,
raw files, PyPI simple pages...) to both servers and compares what clients care about: the status, the size, the
sha256 of the body (optionally after normalising rewritten base URLs and JSON formatting), the Docker-Content-Digest
and the media type, plus any JSON values named by a JSON Pointer (RFC 6901). Prints a report table and exits 0 when
every check matches, 1 when any differs or fails, 2 on a usage or checks-file error.

Standard library only (Python 3.11+), so it runs in any Python image::

    docker run --rm -v "$PWD/Build/migration:/m:ro" -e NEXUS_USERNAME -e NEXUS_PASSWORD -e WAYSTATION_TOKEN \\
      python:3.14-slim python /m/nexus_compare.py --nexus https://nexus.example.com \\
      --waystation https://registry.example.com --checks /m/checks.example.toml

Credentials come from the environment, never the command line: ``NEXUS_USERNAME``/``NEXUS_PASSWORD`` (basic auth)
and ``WAYSTATION_USERNAME`` (any name, default ``validate``)/``WAYSTATION_TOKEN`` (an API token with the read
scope). Unset = anonymous. A Docker ``401`` with a ``Bearer`` challenge is answered by fetching a token from the
challenge's realm (with the side's credentials, if any) and retrying once, as docker clients do. Redirects are
followed; credentials are never sent to another host.

Checks file (TOML)::

    [[check]]
    name = "alpine 3.20 index"            # shown in the report
    format = "docker"                      # free text, for grouping and --only
    method = "HEAD"                        # GET (default) or HEAD
    path = "/repository/docker-group/v2/library/alpine/manifests/3.20"
    headers = { Accept = "application/vnd.oci.image.index.v1+json" }
    # optional:
    waystation_path = "/repository/..."    # when the path differs on the Waystation side
    compare = ["status", "docker_digest"]  # default: status, size, digest (GET), docker_digest, content_type
    normalise = ["base_url", "json"]       # before hashing: base URLs → {BASE}, JSON re-serialised canonically
    json = ["/dist-tags/latest"]           # JSON Pointers whose values must be equal (GET)
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import hashlib
import json
import os
import re
import ssl
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from http.client import HTTPResponse
from pathlib import Path
from typing import Any, TextIO

COMPARISONS = ('status', 'size', 'digest', 'docker_digest', 'content_type')
NORMALISERS = ('base_url', 'json')
DEFAULT_COMPARE = COMPARISONS
CHUNK = 1 << 20
NORMALISE_LIMIT = 64 << 20  # bodies normalised or read as JSON are held in memory: metadata only
USER_AGENT = 'waystation-nexus-compare/1'
EXIT_OK, EXIT_DIFFERENT, EXIT_USAGE = 0, 1, 2


class ChecksError(Exception):
    """The checks file or the command line is wrong (exit 2)."""


# ---------------------------------------------------------------------------- configuration
@dataclass(frozen=True)
class Check:
    name: str
    path: str
    format: str = ''
    method: str = 'GET'
    headers: Mapping[str, str] = field(default_factory=dict)
    waystation_path: str | None = None
    compare: tuple[str, ...] = DEFAULT_COMPARE
    normalise: tuple[str, ...] = ()
    json: tuple[str, ...] = ()


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ChecksError(f'{where} must be a list of strings')
    return tuple(value)


def parse_checks(doc: Mapping[str, Any]) -> list[Check]:
    raw = doc.get('check')
    if not isinstance(raw, list) or not raw:
        raise ChecksError('the checks file needs at least one [[check]] table')
    checks: list[Check] = []
    for i, item in enumerate(raw):
        where = f'check #{i + 1}'
        if not isinstance(item, dict):
            raise ChecksError(f'{where} is not a table')
        unknown = set(item) - set(Check.__dataclass_fields__)
        if unknown:
            raise ChecksError(f'{where}: unknown keys {", ".join(sorted(unknown))}')
        path = item.get('path')
        if not isinstance(path, str) or not path.startswith('/'):
            raise ChecksError(f'{where}: path must start with /')
        ws_path = item.get('waystation_path')
        if ws_path is not None and (not isinstance(ws_path, str) or not ws_path.startswith('/')):
            raise ChecksError(f'{where}: waystation_path must start with /')
        method = str(item.get('method', 'GET')).upper()
        if method not in ('GET', 'HEAD'):
            raise ChecksError(f'{where}: method must be GET or HEAD (the comparison never writes)')
        headers = item.get('headers', {})
        if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
            raise ChecksError(f'{where}: headers must be a table of strings')
        if any(k.lower() == 'authorization' for k in headers):
            raise ChecksError(f'{where}: credentials come from the environment, not the checks file')
        compare = _strings(item.get('compare', list(DEFAULT_COMPARE)), f'{where}: compare')
        if bad := [c for c in compare if c not in COMPARISONS]:
            raise ChecksError(f'{where}: unknown comparison {bad[0]!r} (one of {", ".join(COMPARISONS)})')
        normalise = _strings(item.get('normalise', []), f'{where}: normalise')
        if bad := [n for n in normalise if n not in NORMALISERS]:
            raise ChecksError(f'{where}: unknown normaliser {bad[0]!r} (one of {", ".join(NORMALISERS)})')
        pointers = _strings(item.get('json', []), f'{where}: json')
        if bad := [p for p in pointers if p and not p.startswith('/')]:
            raise ChecksError(f'{where}: JSON Pointer {bad[0]!r} must start with /')
        if method == 'HEAD' and (pointers or normalise):
            raise ChecksError(f'{where}: json and normalise need a GET')
        checks.append(
            Check(
                name=str(item.get('name') or path),
                path=path,
                format=str(item.get('format', '')),
                method=method,
                headers={str(k): v for k, v in headers.items()},
                waystation_path=ws_path,
                compare=compare,
                normalise=normalise,
                json=pointers,
            )
        )
    return checks


def load_checks(path: str) -> list[Check]:
    try:
        with Path(path).open('rb') as fh:
            doc = tomllib.load(fh)
    except OSError as exc:
        raise ChecksError(f'cannot read {path}: {exc.strerror}') from None
    except tomllib.TOMLDecodeError as exc:
        raise ChecksError(f'{path} is not valid TOML: {exc}') from None
    return parse_checks(doc)


@dataclass(frozen=True)
class Side:
    label: str
    base_url: str  # no trailing slash
    public_url: str  # the base URL as it appears in rewritten metadata
    username: str | None = None
    secret: str | None = None

    def url(self, path: str) -> str:
        return self.base_url + path

    def basic(self) -> str | None:
        if self.secret is None:
            return None
        raw = f'{self.username or ""}:{self.secret}'.encode()
        return 'Basic ' + base64.b64encode(raw).decode()


def _base(url: str, option: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.netloc or parts.username or parts.password:
        raise ChecksError(f'{option} must be an http(s) URL without credentials')
    return url.rstrip('/')


# ---------------------------------------------------------------------------- fetching
@dataclass
class Observed:
    status: int | None = None
    size: int | None = None
    sha256: str | None = None
    docker_digest: str | None = None
    content_type: str | None = None
    json: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    elapsed_ms: int = 0


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5


class Fetcher:
    def __init__(self, *, timeout: float, max_bytes: int, context: ssl.SSLContext | None) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        handlers: list[urllib.request.BaseHandler] = [_Redirects()]
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self.opener = urllib.request.build_opener(*handlers)

    def _open(self, url: str, method: str, headers: Mapping[str, str], auth: str | None) -> HTTPResponse:
        if not url.startswith(('http://', 'https://')):
            raise ValueError(f'not an http(s) URL: {url}')
        request = urllib.request.Request(url, method=method, headers={'User-Agent': USER_AGENT, **headers})  # noqa: S310 - checked above
        if auth is not None:
            request.add_unredirected_header('Authorization', auth)  # never forwarded to a redirect target
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:  # an answer all the same: compare it
            response = exc
        assert isinstance(response, (HTTPResponse, urllib.error.HTTPError))
        return response  # type: ignore[return-value]

    def _bearer(self, challenge: str, side: Side) -> str | None:
        """A Docker registry token for a ``WWW-Authenticate: Bearer realm=...`` challenge."""
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.pop('realm', '')
        if not realm.startswith(('http://', 'https://')):
            return None
        query = urllib.parse.urlencode({k: v for k, v in params.items() if k in ('service', 'scope')})
        response = self._open(f'{realm}?{query}' if query else realm, 'GET', {}, side.basic())
        with response:
            if response.status != 200:
                return None
            body = json.loads(response.read(1 << 20) or b'{}')
        token = body.get('token') or body.get('access_token')
        return f'Bearer {token}' if isinstance(token, str) and token else None

    def fetch(self, side: Side, check: Check) -> Observed:
        started = time.perf_counter()
        observed = Observed()
        path = check.waystation_path if side.label == 'waystation' and check.waystation_path else check.path
        try:
            auth = side.basic()
            response = self._open(side.url(path), check.method, check.headers, auth)
            challenge = response.headers.get('WWW-Authenticate', '')
            if response.status == 401 and challenge.lower().startswith('bearer '):
                response.close()
                token = self._bearer(challenge, side)
                if token is not None:
                    response = self._open(side.url(path), check.method, check.headers, token)
            with response:
                self._observe(response, side, check, observed)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            observed.error = _reason(exc)
        observed.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return observed

    def _observe(self, response: HTTPResponse, side: Side, check: Check, observed: Observed) -> None:
        observed.status = response.status
        observed.docker_digest = response.headers.get('Docker-Content-Digest')
        observed.content_type = media_type(response.headers.get('Content-Type'))
        length = response.headers.get('Content-Length')
        if check.method == 'HEAD':
            observed.size = int(length) if length and length.isdigit() else None
            return
        keep = bool(check.normalise or check.json)
        digest, size, held = hashlib.sha256(), 0, bytearray()
        while chunk := response.read(CHUNK):
            size += len(chunk)
            if size > self.max_bytes:
                raise ValueError(f'body larger than --max-bytes ({self.max_bytes})')
            if keep:
                if size > NORMALISE_LIMIT:
                    raise ValueError('body too large to normalise or read as JSON (metadata only)')
                held += chunk
            else:
                digest.update(chunk)
        observed.size = size
        if not keep:
            observed.sha256 = digest.hexdigest()
            return
        body = bytes(held)
        if check.json:
            doc = json.loads(body)
            observed.json = {pointer: resolve_pointer(doc, pointer) for pointer in check.json}
        observed.sha256 = hashlib.sha256(normalise(body, check.normalise, side.public_url)).hexdigest()


def _reason(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        return f'connection failed: {exc.reason}'
    if isinstance(exc, TimeoutError):
        return 'timed out'
    return f'{type(exc).__name__}: {exc}'


def media_type(value: str | None) -> str | None:
    if not value:
        return None
    return value.split(';', 1)[0].strip().lower() or None


_MISSING = object()


def resolve_pointer(doc: Any, pointer: str) -> Any:
    """RFC 6901; a missing member resolves to the string ``<missing>`` (so both sides can agree it is absent)."""
    node = doc
    for token in pointer.split('/')[1:] if pointer else []:
        token = token.replace('~1', '/').replace('~0', '~')
        if isinstance(node, dict):
            node = node.get(token, _MISSING)
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            node = _MISSING
        if node is _MISSING:
            return '<missing>'
    return node


def normalise(body: bytes, how: Sequence[str], public_url: str) -> bytes:
    if 'base_url' in how:
        body = body.replace(public_url.encode(), b'{BASE}')
    if 'json' in how:
        with contextlib.suppress(ValueError):  # not JSON: hash as is
            body = json.dumps(json.loads(body), sort_keys=True, separators=(',', ':')).encode()
    return body


# ---------------------------------------------------------------------------- comparing
@dataclass
class Result:
    check: Check
    nexus: Observed
    waystation: Observed
    differences: list[str]

    @property
    def outcome(self) -> str:
        if self.nexus.error or self.waystation.error:
            return 'ERROR'
        return 'DIFF' if self.differences else 'OK'


def compare(check: Check, nexus: Observed, waystation: Observed) -> list[str]:
    if nexus.error or waystation.error:
        return [f'{label}: {o.error}' for label, o in (('nexus', nexus), ('waystation', waystation)) if o.error]
    out: list[str] = []
    for what in check.compare:
        if what == 'digest' and check.method == 'HEAD':
            continue
        if what == 'docker_digest' and nexus.docker_digest is None and waystation.docker_digest is None:
            continue
        key = 'sha256' if what == 'digest' else what
        left, right = getattr(nexus, key), getattr(waystation, key)
        if left != right:
            out.append(f'{what}: nexus {_short(left)} != waystation {_short(right)}')
        if what == 'status' and left != right:
            return out  # the rest only means something for the same answer
    for pointer in check.json:
        left, right = nexus.json.get(pointer), waystation.json.get(pointer)
        if left != right:
            out.append(f'json {pointer}: nexus {_short(left)} != waystation {_short(right)}')
    return out


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    if isinstance(value, str) and re.fullmatch(r'(sha256:)?[0-9a-f]{64}', value):
        text = value[:19] if value.startswith('sha256:') else value[:12]
    return text if len(text) <= 60 else text[:57] + '...'


def run(checks: Sequence[Check], nexus: Side, waystation: Side, fetcher: Fetcher, parallel: int) -> list[Result]:
    def one(check: Check) -> Result:
        left, right = fetcher.fetch(nexus, check), fetcher.fetch(waystation, check)
        return Result(check, left, right, compare(check, left, right))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        return list(pool.map(one, checks))


# ---------------------------------------------------------------------------- reporting
def _status(o: Observed) -> str:
    return 'error' if o.error else str(o.status)


def rows(results: Sequence[Result]) -> list[list[str]]:
    out = []
    for r in results:
        detail = '; '.join(r.differences) if r.differences else _summary(r.nexus)
        out.append([r.outcome, r.check.format, r.check.name, _status(r.nexus), _status(r.waystation), detail])
    return out


def _summary(o: Observed) -> str:
    parts = [p for p in (o.docker_digest and _short(o.docker_digest), o.size is not None and f'{o.size} B') if p]
    return ', '.join(str(p) for p in parts)


HEADER = ['RESULT', 'FORMAT', 'CHECK', 'NEXUS', 'WAYSTATION', 'DETAIL']


def write_table(results: Sequence[Result], out: TextIO) -> None:
    table = [HEADER, *rows(results)]
    widths = [max(len(row[i]) for row in table) for i in range(len(HEADER) - 1)]
    for row in table:
        out.write('  '.join(cell.ljust(widths[i]) for i, cell in enumerate(row[:-1])) + '  ' + row[-1] + '\n')
    out.write('\n' + summary_line(results) + '\n')


def write_markdown(results: Sequence[Result], out: TextIO) -> None:
    def cell(text: str) -> str:
        return text.replace('|', '\\|').replace('\n', ' ')

    out.write('| ' + ' | '.join(HEADER) + ' |\n|' + '---|' * len(HEADER) + '\n')
    for row in rows(results):
        out.write('| ' + ' | '.join(cell(c) for c in row) + ' |\n')
    out.write('\n' + summary_line(results) + '\n')


def write_json(results: Sequence[Result], out: TextIO) -> None:
    doc = {
        'summary': counts(results),
        'results': [
            {
                'name': r.check.name,
                'format': r.check.format,
                'method': r.check.method,
                'path': r.check.path,
                'waystation_path': r.check.waystation_path or r.check.path,
                'outcome': r.outcome,
                'differences': r.differences,
                'nexus': asdict(r.nexus),
                'waystation': asdict(r.waystation),
            }
            for r in results
        ],
    }
    json.dump(doc, out, indent=2, default=str)
    out.write('\n')


def counts(results: Sequence[Result]) -> dict[str, int]:
    out = {'total': len(results), 'ok': 0, 'diff': 0, 'error': 0}
    for r in results:
        out[r.outcome.lower()] += 1
    return out


def summary_line(results: Sequence[Result]) -> str:
    c = counts(results)
    return f'{c["total"]} checks: {c["ok"]} match, {c["diff"]} differ, {c["error"]} failed'


# ---------------------------------------------------------------------------- command line
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Send the same requests to a Nexus and a Waystation and compare the answers.',
        epilog='Credentials: NEXUS_USERNAME/NEXUS_PASSWORD, WAYSTATION_USERNAME/WAYSTATION_TOKEN (environment).',
    )
    parser.add_argument('--nexus', required=True, help='Nexus base URL, e.g. https://nexus.example.com')
    parser.add_argument('--waystation', required=True, help='Waystation base URL, e.g. https://registry.example.com')
    parser.add_argument('--checks', required=True, help='TOML file of [[check]] tables')
    parser.add_argument('--only', default='', help='comma-separated formats to run (default: all)')
    parser.add_argument('--report', choices=('table', 'markdown', 'json'), default='table')
    parser.add_argument('--output', help='write the report to this file as well as standard output')
    parser.add_argument('--ca-file', help='PEM bundle of CAs to trust instead of the system store (private CAs)')
    parser.add_argument('--timeout', type=float, default=60.0, help='seconds per request (default 60)')
    parser.add_argument('--max-bytes', type=int, default=4 << 30, help='largest body to hash (default 4 GiB)')
    parser.add_argument('--parallel', type=int, default=4, help='checks run at once (default 4)')
    parser.add_argument('--nexus-public-url', help='Nexus base URL as written in rewritten metadata (default --nexus)')
    parser.add_argument(
        '--waystation-public-url', help='Waystation base URL as written in rewritten metadata (default --waystation)'
    )
    return parser


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, out: TextIO | None = None) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    args = build_parser().parse_args(argv)
    try:
        checks = load_checks(args.checks)
        nexus_url, ws_url = _base(args.nexus, '--nexus'), _base(args.waystation, '--waystation')
        nexus = Side(
            'nexus',
            nexus_url,
            _base(args.nexus_public_url or nexus_url, '--nexus-public-url'),
            env.get('NEXUS_USERNAME'),
            env.get('NEXUS_PASSWORD') or None,
        )
        waystation = Side(
            'waystation',
            ws_url,
            _base(args.waystation_public_url or ws_url, '--waystation-public-url'),
            env.get('WAYSTATION_USERNAME') or 'validate',
            env.get('WAYSTATION_TOKEN') or None,
        )
        context = ssl.create_default_context(cafile=args.ca_file) if args.ca_file else None
    except (ChecksError, OSError, ssl.SSLError) as exc:
        sys.stderr.write(f'nexus_compare: {exc}\n')
        return EXIT_USAGE
    if args.only:
        wanted = {f.strip() for f in args.only.split(',') if f.strip()}
        checks = [c for c in checks if c.format in wanted]
        if not checks:
            sys.stderr.write(f'nexus_compare: no checks for {", ".join(sorted(wanted))}\n')
            return EXIT_USAGE
    fetcher = Fetcher(timeout=args.timeout, max_bytes=args.max_bytes, context=context)
    results = run(checks, nexus, waystation, fetcher, args.parallel)
    writer = {'table': write_table, 'markdown': write_markdown, 'json': write_json}[args.report]
    writer(results, out)
    if args.output:
        with Path(args.output).open('w', encoding='utf-8') as fh:
            writer(results, fh)
    return EXIT_OK if all(r.outcome == 'OK' for r in results) else EXIT_DIFFERENT


if __name__ == '__main__':
    sys.exit(main())
