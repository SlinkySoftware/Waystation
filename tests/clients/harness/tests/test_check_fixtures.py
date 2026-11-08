"""The forbidden-strings check must block planted environment details.

Planted values are assembled at runtime so this file itself passes the check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from check_fixtures import check, host_allowed, ip_allowed, main, scan_bytes

ALLOW = ['example.com', 'docker.io', 'registry.npmjs.org']
INTERNAL_HOST = 'nexus' + '.corp' + '.lan'
PRIVATE_IP = '10.20.' + '30.40'


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    clients = tmp_path / 'clients'
    (clients / 'fmt').mkdir(parents=True)
    (clients / 'allowed_hosts.txt').write_text('# comment\nexample.com\ndocker.io\n')
    (clients / 'fmt' / 'format.env').write_text('HOSTS="echo.example.com"\nFIXTURE_BUDGET_KB=1\n')
    fixtures = tmp_path / 'fixtures'
    (fixtures / 'fmt' / 'echo.example.com').mkdir(parents=True)
    return clients, fixtures


def test_allowlist_matching() -> None:
    assert host_allowed('registry-1.docker.io', ALLOW)
    assert host_allowed('example.com', ALLOW)
    assert host_allowed('anything.example', [])
    assert not host_allowed('not' + 'example.com', ALLOW)
    assert not host_allowed('registry.example.com' + '.lan', ALLOW)  # internal suffix beats the allowlist


def test_ip_classification() -> None:
    assert ip_allowed('192.0.2.7') is True
    assert ip_allowed('2001:db8::1') is True
    assert ip_allowed('127.0.0.1') is True
    assert ip_allowed(PRIVATE_IP) is False
    assert ip_allowed('172.' + '16.0.1') is False
    assert ip_allowed('fd00' + '::1') is False
    assert ip_allowed('not-an-ip') is None


@pytest.mark.parametrize(
    'planted',
    [
        f'{{"upstream": "https://{INTERNAL_HOST}/repository/npm/"}}',
        'mirror: registry.' + 'internal',
        'contact ops@mycompany' + '.com.au',
        f'client={PRIVATE_IP}',
        'X-Forwarded-For: ' + '100.' + '64.1.2',
        'via fe80:' + ':1:2',
        '-----BEGIN ' + 'OPENSSH PRIVATE KEY-----',
        'token ghp_' + 'A' * 36,
        'npm_' + 'b' * 36,
        'eyJ' + 'h' * 10 + '.eyJ' + 'p' * 10 + '.' + 's' * 10,
    ],
)
def test_planted_values_are_rejected(planted: str) -> None:
    assert list(scan_bytes(planted.encode(), ALLOW, []))


@pytest.mark.parametrize(
    'clean',
    [
        '{"tarball": "https://registry.npmjs.org/is-number/-/is-number-7.0.0.tgz", "version": "7.0.0"}',
        'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"',
        'logger.info("x"); package.json index.js run.sh README.md sha256:0123abcd 12:34:56',
        'listen 192.0.2.1 and 2001:db8::5 and 127.0.0.1 and ::1',
        '"escaped\\nregistry.npmjs.org"',  # a JSON newline escape is not part of the host
    ],
)
def test_clean_text_passes(clean: str) -> None:
    assert list(scan_bytes(clean.encode(), ALLOW, [])) == []


def test_binary_bodies_skip_host_scan_but_not_key_scan() -> None:
    binary = b'\x00\x1f\x8b' + INTERNAL_HOST.encode()
    assert list(scan_bytes(binary, ALLOW, [])) == []
    assert list(scan_bytes(binary + b'-----BEGIN ' + b'PRIVATE KEY-----', ALLOW, []))


def test_local_deny_list() -> None:
    findings = list(scan_bytes(b'see Example.COM/thing', ALLOW, ['example.com']))
    assert findings == ['contains a string from the local deny list']


def test_check_blocks_planted_fixture_and_budget(layout: tuple[Path, Path]) -> None:
    clients, fixtures = layout
    good = fixtures / 'fmt' / 'echo.example.com' / 'get-hello-0.json'
    good.write_text('{"url": "https://echo.example.com/hello"}\n')
    assert check([clients, fixtures], clients, fixtures) == []

    planted = fixtures / 'fmt' / 'echo.example.com' / 'get-x-1.json'
    planted.write_text(f'{{"url": "https://{INTERNAL_HOST}/x"}}\n')
    findings = check([fixtures], clients, fixtures)
    assert [f.path for f in findings] == [planted]

    planted.unlink()
    (fixtures / 'fmt' / 'echo.example.com' / 'big.body').write_bytes(b'\x00' * 2048)
    findings = check([fixtures], clients, fixtures)
    assert len(findings) == 1
    assert 'budget' in findings[0].message


def test_per_format_allowlist(layout: tuple[Path, Path]) -> None:
    clients, fixtures = layout
    body = fixtures / 'fmt' / 'echo.example.com' / 'get-pkg-2.body'
    body.write_text('{"homepage": "https://' + 'github' + '.com/someone/is-number"}')
    assert len(check([fixtures], clients, fixtures)) == 1
    (clients / 'fmt' / 'allowed_hosts.txt').write_text('github' + '.com\n')
    assert check([fixtures], clients, fixtures) == []


def test_main_exit_codes(layout: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    clients, fixtures = layout
    args = ['--clients-dir', str(clients), '--fixtures-root', str(fixtures)]
    assert main(args) == 0
    (fixtures / 'fmt' / 'echo.example.com' / 'planted.json').write_text(f'"{PRIVATE_IP}"')
    assert main(args) == 1
    assert 'FORBIDDEN' in capsys.readouterr().err
