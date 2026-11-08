"""Forbidden-strings and size check for upstream fixtures and the harness itself (tests/clients/README.md).

Fails (exit 1) when a scanned file contains:
- a host name outside the allowlist (``tests/clients/allowed_hosts.txt`` plus, for files under
  ``tests/fixtures/upstreams/<format>/``, ``tests/clients/<format>/allowed_hosts.txt``), or any host under an
  internal-only suffix, which no allowlist can permit;
- an IP address outside the documentation ranges (RFC 5737, RFC 3849), loopback and unspecified addresses;
- private key material or a well-known token shape (GitHub, npm, GitLab, Slack, AWS, JWT);
- any string from the optional local deny list ``$WAYSTATION_FORBIDDEN_FILE`` (one string per line, kept outside
  the repo, e.g. the owner's real domains);
- more bytes than the format's budget (``FIXTURE_BUDGET_KB`` in ``tests/clients/<format>/format.env``, default
  8192 KiB) or a single file over ``FIXTURE_FILE_MAX_KB`` (default 4096 KiB).

Host names and IPs are only searched in text files (UTF-8, no NUL); binary bodies (compressed layers, tarballs)
are scanned for key and token shapes only. Stdlib only; runs in the dev image via ``make clients-check``.
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

# Final labels treated as host names. Deliberately excludes labels that collide with file extensions in normal text
# (js, py, md, sh, rs, pl, so, ps, gz, info as in logger.info, ...): those would drown the check in false positives.
TLDS = frozenset(
    {
        # common public
        *('com', 'net', 'org', 'io', 'dev', 'app', 'co', 'cloud', 'ai', 'biz', 'me', 'tech', 'xyz', 'online'),
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
    r'(?<![0-9.])((?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3})'
    r'(?![0-9]|\.\d)'
)
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


def scan_text(text: str, allowlist: Iterable[str]) -> Iterator[str]:
    allow = list(allowlist)
    text = ESCAPE_RE.sub(' ', text)  # a literal backslash-n before a host name is a newline, not a letter
    for match in HOST_RE.finditer(text):
        host = match.group(1).lower()
        if host.rsplit('.', 1)[-1] in TLDS and not host_allowed(host, allow):
            yield f'host name not in the allowlist: {host}'
    for match in IPV4_RE.finditer(text):
        if ip_allowed(match.group(1)) is False:
            yield f'IP address outside the documentation ranges: {match.group(1)}'
    for match in IPV6_RE.finditer(text):
        if ip_allowed(match.group(1)) is False:  # most hex:hex:hex tokens (times, digests) are not addresses
            yield f'IP address outside the documentation ranges: {match.group(1)}'


def scan_bytes(data: bytes, allowlist: Iterable[str], deny: Iterable[str]) -> Iterator[str]:
    for name, pattern in SECRET_RES.items():
        if pattern.search(data):
            yield f'looks like a {name}'
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
    yield from scan_text(text, allowlist)


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


def check(paths: Iterable[Path], clients_dir: Path, fixtures_root: Path, deny: Iterable[str] = ()) -> list[Finding]:
    base_allow = read_list(clients_dir / 'allowed_hosts.txt')
    deny = [d.lower() for d in deny]
    findings: list[Finding] = []
    totals: dict[str, int] = {}
    file_max = DEFAULT_FILE_MAX_KB * 1024
    for root in paths:
        for path in iter_files(root):
            data = path.read_bytes()
            fmt = fixture_format(path, fixtures_root)
            allow = base_allow
            if fmt is not None:
                allow = base_allow + read_list(clients_dir / fmt / 'allowed_hosts.txt')
                totals[fmt] = totals.get(fmt, 0) + len(data)
                env = read_env(clients_dir / fmt / 'format.env')
                file_max = int(env.get('FIXTURE_FILE_MAX_KB', DEFAULT_FILE_MAX_KB)) * 1024
                if len(data) > file_max:
                    findings.append(Finding(path, f'{len(data)} bytes exceeds the per-file limit ({file_max})'))
            findings.extend(Finding(path, msg) for msg in dict.fromkeys(scan_bytes(data, allow, deny)))
    for fmt, total in sorted(totals.items()):
        env = read_env(clients_dir / fmt / 'format.env')
        budget = int(env.get('FIXTURE_BUDGET_KB', DEFAULT_BUDGET_KB)) * 1024
        if total > budget:
            msg = f'fixtures total {total} bytes, over the format budget of {budget} (FIXTURE_BUDGET_KB)'
            findings.append(Finding(fixtures_root / fmt, msg))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('paths', nargs='*', type=Path, help='files/directories (default: harness + all fixtures)')
    parser.add_argument('--clients-dir', type=Path, default=Path('tests/clients'))
    parser.add_argument('--fixtures-root', type=Path, default=Path('tests/fixtures/upstreams'))
    args = parser.parse_args(argv)
    paths = args.paths or [args.clients_dir, args.fixtures_root]
    deny_file = os.environ.get('WAYSTATION_FORBIDDEN_FILE')
    deny = read_list(Path(deny_file)) if deny_file else []
    findings = check(paths, args.clients_dir, args.fixtures_root, deny)
    for finding in findings:
        print(f'FORBIDDEN {finding}', file=sys.stderr)
    if findings:
        print(f'check_fixtures: {len(findings)} problem(s)', file=sys.stderr)
        return 1
    print(f'check_fixtures: ok ({", ".join(str(p) for p in paths)})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
