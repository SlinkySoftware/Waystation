"""Forbidden-strings check: the whole repository (``Build/ci/forbidden_strings.sh``) and, with size budgets, the
upstream fixtures and the client harness (``make clients-check``, tests/clients/README.md).

Fails (exit 1) when a scanned file contains:
- a host name outside the allowlist (``Build/ci/allowed_hosts.txt`` plus, for files under
  ``tests/fixtures/upstreams/<format>/`` or ``tests/clients/<format>/``, ``tests/clients/<format>/allowed_hosts.txt``),
  or any host under an internal-only suffix, which no allowlist can permit. ``url <prefix>`` lines in the allowlist
  permit one exact URL prefix (the two source-control URLs) without allowing its host anywhere else;
- an IP address outside the documentation ranges (RFC 5737, RFC 3849), loopback and unspecified addresses;
- private key material or a well-known token shape (GitHub, npm, GitLab, Slack, AWS, JWT);
- any string from the optional local deny list ``$WAYSTATION_FORBIDDEN_FILE`` (one string per line, kept outside
  the repo, e.g. the owner's real domains);
- more bytes than the format's budget (``FIXTURE_BUDGET_KB`` in ``tests/clients/<format>/format.env``, default
  8192 KiB) or a single file over ``FIXTURE_FILE_MAX_KB`` (default 4096 KiB).

Host names and IPs are only searched in text files (UTF-8, no NUL); binary bodies (compressed layers, tarballs,
images) are scanned for key and token shapes only. Repository mode (``--files-from``) scans every listed file except
``REPO_SKIP`` (generated or vendored files whose sources are scanned anyway). Stdlib only; runs in the dev image.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BUDGET_KB = 8192
DEFAULT_FILE_MAX_KB = 4096
SKIP_DIRS = frozenset({'recordings-tmp', '.run', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'})
DEFAULT_ALLOWLIST = Path('Build/ci/allowed_hosts.txt')
# Repository mode: generated files (their sources are scanned) and files that are not ours to edit.
REPO_SKIP = (
    'Application/Web/src/api/schema.d.ts',  # generated from openapi/waystation-m0.yaml
    'Application/Web/public/mockServiceWorker.js',  # vendored by msw
)

# Final labels treated as host names. Deliberately excludes labels that collide with file extensions in normal text
# (js, py, md, sh, rs, pl, so, ps, gz, info as in logger.info, ...) or with attribute access in code (app as in
# self.app, online as in repo.online): those would drown the check in false positives.
TLDS = frozenset(
    {
        # common public
        *('com', 'net', 'org', 'io', 'dev', 'co', 'cloud', 'ai', 'biz', 'me', 'tech', 'xyz'),
        *('site', 'au', 'uk', 'nz', 'de', 'eu', 'us', 'ca', 'fr', 'nl', 'jp'),
        # internal-only and reserved
        *('local', 'lan', 'internal', 'intranet', 'corp', 'home', 'private', 'localdomain', 'arpa'),
        *('example', 'test', 'invalid', 'localhost'),
    }
)
# Never allowed, whatever the allowlist says.
INTERNAL_SUFFIXES = (
    '.local',
    '.lan',
    '.internal',
    '.intranet',
    '.corp',
    '.home',
    '.private',
    '.localdomain',
    '.home.arpa',
)
# Reserved names (RFC 2606 / RFC 6761) that can never identify a real environment.
RESERVED_SUFFIXES = ('.example', '.invalid', '.test', '.localhost')

ALLOWED_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        '192.0.2.0/24',
        '198.51.100.0/24',
        '203.0.113.0/24',
        '127.0.0.0/8',
        '0.0.0.0/32',
        '2001:db8::/32',
        '::1/128',
        '::/128',
    )
)

HOST_RE = re.compile(
    r'(?<![A-Za-z0-9_.-])((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63})'
    r'(?![A-Za-z0-9_-])'
)
IPV4_RE = re.compile(
    r'(?<![0-9.-])((?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3})'
    r'(?![0-9]|\.\d)'
)
# Definitions of special-purpose ranges written as CIDR (e.g. an SSRF guard's deny list) are not environment details.
WELL_KNOWN_BLOCKS = frozenset(
    ipaddress.ip_network(n)
    for n in (
        *('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12'),
        *('192.168.0.0/16', '224.0.0.0/4', '240.0.0.0/4', '255.255.255.255/32'),
        *('::/128', '::1/128', '::ffff:0:0/96', '64:ff9b::/96', 'fc00::/7', 'fe80::/10', 'ff00::/8'),
    )
)
CIDR_SUFFIX_RE = re.compile(r'/(\d{1,3})(?!\d)')
# A line containing this marker is skipped by the host/IP scan (never the key/token scan): for tests that must use a
# non-documentation address, such as SSRF guard tests. Reviewers check every use.
PRAGMA = 'forbidden-strings: allow'
# Lockfiles are scanned for hosts only on the lines that say where a package is downloaded from: a private registry
# URL there is a leak, funding and homepage links are not.
LOCKFILE_URL_LINES = {
    'package-lock.json': re.compile(r'"resolved"\s*:'),
    'uv.lock': re.compile(r'\b(?:registry|url|git|path)\s*='),
}
IPV6_RE = re.compile(r'(?<![0-9A-Za-z:])([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})(?![0-9A-Za-z:])')
ESCAPE_RE = re.compile(r'\\[nrtfb]')
SECRET_RES = {
    'private key': re.compile(rb'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----'),
    'GitHub token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,})'),
    'npm token': re.compile(rb'\bnpm_[A-Za-z0-9]{36}\b'),
    'GitLab token': re.compile(rb'\bglpat-[A-Za-z0-9_-]{20}'),
    'Slack token': re.compile(rb'\bxox[abposr]-[A-Za-z0-9-]{10,}'),
    'AWS access key': re.compile(rb'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'),
    'JWT': re.compile(rb'\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'),
}


@dataclass(frozen=True)
class Finding:
    path: Path
    message: str

    def __str__(self) -> str:
        return f'{self.path}: {self.message}'


@dataclass(frozen=True)
class Allowlist:
    """``Build/ci/allowed_hosts.txt``: host entries, ``url <prefix>`` and ``not-a-host <token>`` lines."""

    hosts: tuple[str, ...]
    urls: tuple[str, ...] = ()
    not_hosts: frozenset[str] = frozenset()

    def extend(self, hosts: Iterable[str]) -> Allowlist:
        return Allowlist((*self.hosts, *hosts), self.urls, self.not_hosts)


def read_allowlist(path: Path) -> Allowlist:
    hosts: list[str] = []
    urls: list[str] = []
    not_hosts: set[str] = set()
    for entry in read_list(path):
        kind, _, value = entry.partition(' ')
        if kind == 'url' and value:
            urls.append(value.strip())
        elif kind == 'not-a-host' and value:
            not_hosts.add(value.strip())
        else:
            hosts.append(entry)
    return Allowlist(tuple(hosts), tuple(urls), frozenset(not_hosts))


def read_list(path: Path) -> list[str]:
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text().splitlines():
        line = line.split('#', 1)[0].strip().lower()
        if line:
            entries.append(line)
    return entries


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in read_list(path):
        key, sep, value = line.partition('=')
        if sep:
            env[key.strip().upper()] = value.strip().strip('"\'')
    return env


def host_allowed(host: str, allowlist: Iterable[str]) -> bool:
    host = host.lower().rstrip('.')
    if host.endswith(INTERNAL_SUFFIXES):
        return False
    if host.endswith(RESERVED_SUFFIXES):
        return True
    return any(host == entry or host.endswith('.' + entry) for entry in allowlist)


def ip_allowed(text: str) -> bool | None:
    """True/False for an address, None when ``text`` is not an IP address at all."""
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    return any(addr in net for net in ALLOWED_NETS if net.version == addr.version)


def well_known_block(text: str, after: str) -> bool:
    """``10.0.0.0/8``-style definitions of the special-purpose ranges (SSRF guards, trusted-proxy defaults)."""
    m = CIDR_SUFFIX_RE.match(after)
    if m is None:
        return False
    try:
        return ipaddress.ip_network(f'{text}/{m.group(1)}') in WELL_KNOWN_BLOCKS
    except ValueError:
        return False


def scan_text(
    text: str, allowlist: Iterable[str] | Allowlist, urls: Iterable[str] = (), *, scan_ipv4: bool = True
) -> Iterator[str]:
    allow = allowlist if isinstance(allowlist, Allowlist) else Allowlist(tuple(allowlist), tuple(urls))
    if PRAGMA in text:  # lines that deliberately hold a non-documentation address (SSRF tests) opt out
        text = '\n'.join(line for line in text.splitlines() if PRAGMA not in line)
    for url in allow.urls:  # an allowed URL prefix is blanked; the rest of the line is still scanned
        text = re.sub(re.escape(url), ' ', text, flags=re.IGNORECASE)
    text = ESCAPE_RE.sub(' ', text)  # a literal backslash-n before a host name is a newline, not a letter
    for match in HOST_RE.finditer(text):
        host = match.group(1).lower()
        if host in allow.not_hosts:
            continue
        if host.rsplit('.', 1)[-1] in TLDS and not host_allowed(host, allow.hosts):
            yield f'host name not in the allowlist: {host}'
    ip_matches = [*(IPV4_RE.finditer(text) if scan_ipv4 else ()), *IPV6_RE.finditer(text)]
    for match in ip_matches:  # most hex:hex:hex tokens (times, digests) are not addresses
        addr = match.group(1)
        if ip_allowed(addr) is False and not well_known_block(addr, text[match.end() : match.end() + 4]):
            yield f'IP address outside the documentation ranges: {addr}'


def scan_bytes(
    data: bytes,
    allowlist: Iterable[str] | Allowlist,
    deny: Iterable[str],
    urls: Iterable[str] = (),
    *,
    name: str = '',
) -> Iterator[str]:
    """Scan one file's bytes; ``name`` (the file name) selects lockfile and SVG handling."""
    for label, pattern in SECRET_RES.items():
        if pattern.search(data):
            yield f'looks like a {label}'
    lowered = data.lower()
    for needle in deny:
        if needle.encode() in lowered:
            yield 'contains a string from the local deny list'
    if b'\x00' in data:
        return
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return
    lock_lines = LOCKFILE_URL_LINES.get(name)
    if lock_lines is not None:  # lockfiles: only where packages come from (no funding/homepage links)
        text = '\n'.join(line for line in text.splitlines() if lock_lines.search(line))
    yield from scan_text(text, allowlist, urls, scan_ipv4=not name.endswith('.svg'))  # SVG path data looks like IPs


def iter_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def fixture_format(path: Path, fixtures_root: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(fixtures_root.resolve())
    except ValueError:
        return None
    return rel.parts[0] if len(rel.parts) > 1 else None


def check(
    paths: Iterable[Path],
    clients_dir: Path,
    fixtures_root: Path,
    deny: Iterable[str] = (),
    allowlist: Path = DEFAULT_ALLOWLIST,
) -> list[Finding]:
    base = read_allowlist(allowlist)
    deny = [d.lower() for d in deny]
    findings: list[Finding] = []
    totals: dict[str, int] = {}
    for root in paths:
        for path in iter_files(root):
            if not path.is_file():  # listed but deleted in the working tree
                continue
            data = path.read_bytes()
            fmt = fixture_format(path, fixtures_root)
            allow = base
            own = fixture_format(path, clients_dir)  # tests/clients/<format>/...: the format's own files
            if own is not None and (clients_dir / own / 'format.env').is_file():
                allow = base.extend(read_list(clients_dir / own / 'allowed_hosts.txt'))
            if fmt is not None:
                allow = base.extend(read_list(clients_dir / fmt / 'allowed_hosts.txt'))
                totals[fmt] = totals.get(fmt, 0) + len(data)
                env = read_env(clients_dir / fmt / 'format.env')
                file_max = int(env.get('FIXTURE_FILE_MAX_KB', DEFAULT_FILE_MAX_KB)) * 1024
                if len(data) > file_max:
                    findings.append(Finding(path, f'{len(data)} bytes exceeds the per-file limit ({file_max})'))
            messages = scan_bytes(data, allow, deny, name=path.name)
            findings.extend(Finding(path, msg) for msg in dict.fromkeys(messages))
    for fmt, total in sorted(totals.items()):
        env = read_env(clients_dir / fmt / 'format.env')
        budget = int(env.get('FIXTURE_BUDGET_KB', DEFAULT_BUDGET_KB)) * 1024
        if total > budget:
            msg = f'fixtures total {total} bytes, over the format budget of {budget} (FIXTURE_BUDGET_KB)'
            findings.append(Finding(fixtures_root / fmt, msg))
    return findings


def read_file_list(source: str) -> list[Path]:
    """NUL- or newline-separated paths (``git ls-files -z``), minus ``REPO_SKIP``."""
    raw = sys.stdin.read() if source == '-' else Path(source).read_text()
    names = [n.strip() for n in raw.replace('\0', '\n').splitlines()]
    return [Path(n) for n in names if n and n not in REPO_SKIP]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('paths', nargs='*', type=Path, help='files/directories (default: harness + all fixtures)')
    parser.add_argument('--clients-dir', type=Path, default=Path('tests/clients'))
    parser.add_argument('--fixtures-root', type=Path, default=Path('tests/fixtures/upstreams'))
    parser.add_argument('--allowlist', type=Path, default=DEFAULT_ALLOWLIST, help='base host allowlist')
    parser.add_argument('--files-from', metavar='FILE', help="also scan the files listed in FILE ('-' = stdin)")
    args = parser.parse_args(argv)
    paths: list[Path] = list(args.paths)
    if args.files_from:
        paths += read_file_list(args.files_from)
        label = f'{len(paths)} files'
    else:
        paths = paths or [args.clients_dir, args.fixtures_root]
        label = ', '.join(str(p) for p in paths)
    deny_file = os.environ.get('WAYSTATION_FORBIDDEN_FILE')
    deny = read_list(Path(deny_file)) if deny_file else []
    findings = check(paths, args.clients_dir, args.fixtures_root, deny, args.allowlist)
    for finding in findings:
        print(f'FORBIDDEN {finding}', file=sys.stderr)
    if findings:
        print(f'forbidden strings: {len(findings)} problem(s)', file=sys.stderr)
        return 1
    print(f'forbidden strings: ok ({label})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
