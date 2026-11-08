"""The forbidden-strings check must block planted environment details.

Planted values are assembled at runtime so this file itself passes the check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from check_fixtures import (
    DEFAULT_ALLOWLIST,
    Allowlist,
    check,
    host_allowed,
    ip_allowed,
    main,
    read_allowlist,
    scan_bytes,
)

ALLOW = ['example.com', 'docker.io', 'registry.npmjs.org']
ALLOWLIST = 'allowed_hosts.txt'
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
    assert check([clients, fixtures], clients, fixtures, allowlist=clients / ALLOWLIST) == []

    planted = fixtures / 'fmt' / 'echo.example.com' / 'get-x-1.json'
    planted.write_text(f'{{"url": "https://{INTERNAL_HOST}/x"}}\n')
    findings = check([fixtures], clients, fixtures, allowlist=clients / ALLOWLIST)
    assert [f.path for f in findings] == [planted]

    planted.unlink()
    (fixtures / 'fmt' / 'echo.example.com' / 'big.body').write_bytes(b'\x00' * 2048)
    findings = check([fixtures], clients, fixtures, allowlist=clients / ALLOWLIST)
    assert len(findings) == 1
    assert 'budget' in findings[0].message


def test_per_format_allowlist(layout: tuple[Path, Path]) -> None:
    clients, fixtures = layout
    body = fixtures / 'fmt' / 'echo.example.com' / 'get-pkg-2.body'
    body.write_text('{"homepage": "https://' + 'github' + '.com/someone/is-number"}')
    assert len(check([fixtures], clients, fixtures, allowlist=clients / ALLOWLIST)) == 1
    (clients / 'fmt' / 'allowed_hosts.txt').write_text('github' + '.com\n')
    assert check([fixtures], clients, fixtures, allowlist=clients / ALLOWLIST) == []
    # The format's own files (its allowlist, test.sh) are scanned with its allowlist too; other formats' are not.
    assert check([clients], clients, fixtures, allowlist=clients / ALLOWLIST) == []
    (clients / 'other').mkdir()
    (clients / 'other' / 'format.env').write_text('HOSTS="echo.example.com"\n')
    (clients / 'other' / 'test.sh').write_text('curl https://' + 'github' + '.com/\n')
    assert len(check([clients], clients, fixtures, allowlist=clients / ALLOWLIST)) == 1


def test_main_exit_codes(layout: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    clients, fixtures = layout
    args = ['--allowlist', str(clients / ALLOWLIST), '--clients-dir', str(clients), '--fixtures-root', str(fixtures)]
    assert main(args) == 0
    (fixtures / 'fmt' / 'echo.example.com' / 'planted.json').write_text(f'"{PRIVATE_IP}"')
    assert main(args) == 1
    assert 'FORBIDDEN' in capsys.readouterr().err


SCM_HOST = 'git' + 'host.example' + '.net.au'  # stands in for the secondary source-control host


def test_allowlist_file_entries(tmp_path: Path) -> None:
    path = tmp_path / ALLOWLIST
    path.write_text(f'example.com\nurl https://{SCM_HOST}/Org/Repo\nnot-a-host nav.home\n')
    allow = read_allowlist(path)
    assert allow == Allowlist(('example.com',), (f'https://{SCM_HOST}/org/repo',), frozenset({'nav.home'}))
    # The exact URL prefix passes; the same host anywhere else does not.
    assert list(scan_bytes(f'clone https://{SCM_HOST}/Org/Repo.git'.encode(), allow, [])) == []
    assert list(scan_bytes(f'https://{SCM_HOST}/Other/Repo'.encode(), allow, []))
    assert list(scan_bytes(f'ssh {SCM_HOST}'.encode(), allow, []))
    # An exact code token is skipped; a real host under the same internal suffix is not.
    assert list(scan_bytes(b"t('nav.home')", allow, [])) == []
    assert list(scan_bytes(b'nas' + b'.home', allow, []))


def test_pragma_skips_host_and_ip_scan_but_not_secrets() -> None:
    marker = 'forbidden-strings' + ': allow'
    assert list(scan_bytes(f"'{PRIVATE_IP}'  # {marker}".encode(), ALLOW, [])) == []
    assert list(scan_bytes(f"'{PRIVATE_IP}'\n# {marker}".encode(), ALLOW, []))  # same line only
    assert list(scan_bytes(f'ghp_{"A" * 36}  # {marker}'.encode(), ALLOW, []))


@pytest.mark.parametrize(
    ('text', 'clean'),
    [
        ("['10." + "0.0.0/8', '172." + "16.0.0/12', 'fc00" + "::/7', 'fe80" + "::/10']", True),
        ("'10." + "20.0.0/16'", False),  # a specific private network is an environment detail
        ("'10." + "0.0.1/8'", False),
        ("vr: '6.12.0-104." + "43.4.3.el9uek'", True),  # a version string, not an address
    ],
)
def test_ip_context(text: str, clean: bool) -> None:
    assert (list(scan_bytes(text.encode(), ALLOW, [])) == []) is clean


def test_lockfiles_scan_download_urls_only() -> None:
    funding = 'https://funding' + '-site.com/a'  # not allowlisted, but a funding link is not a download source
    lock = f'"resolved": "https://registry.npmjs.org/a/-/a-1.0.0.tgz",\n"funding": {{"url": "{funding}"}}\n'
    assert list(scan_bytes(lock.encode(), ALLOW, [], name='package-lock.json')) == []
    private = f'"resolved": "https://{INTERNAL_HOST}/repository/npm/a/-/a-1.0.0.tgz",\n'
    assert list(scan_bytes(private.encode(), ALLOW, [], name='package-lock.json'))
    uv = 'source = { registry = "https://pypi.' + 'corp-example' + '.com/simple" }\n'
    assert list(scan_bytes(uv.encode(), ALLOW, [], name='uv.lock'))


def test_svg_path_data_is_not_an_address() -> None:
    svg = b'<path d="M1.92' + b'.96.96 3.1"/>'
    assert list(scan_bytes(svg, ALLOW, [], name='icon.svg')) == []
    assert list(scan_bytes(svg, ALLOW, []))


def test_repository_allowlist_is_well_formed() -> None:
    allow = read_allowlist(Path(__file__).parents[4] / DEFAULT_ALLOWLIST)
    assert 'example.com' in allow.hosts
    assert len(allow.urls) == 1  # the secondary source-control URL only
    for host in allow.hosts:
        assert not host_allowed(host + '.lan', allow.hosts)  # internal suffixes can never be allowlisted
