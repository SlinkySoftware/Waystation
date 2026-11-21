"""apt refresh (``apt.refresh``) pure parts and the worker's upstream fetcher against a local fake upstream: suite
grouping, ``Packages`` variant choice, which unverified files an index could list, breaker state, credentials only to
the upstream's origin, redirects, size bounds and content encodings."""

import base64
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ws_fake_upstream import FakeUpstream, Respond, Seen
from ws_testing import TEST_KEY, make_settings

from waystation_common.apt import deb822
from waystation_common.crypto import Keyring
from waystation_common.errors import PayloadTooLarge, UpstreamError
from waystation_common.events import keys as event_keys
from waystation_common.jobs.model import AptRefresh
from waystation_worker import apt, upstream
from waystation_worker.jobs import BUILTIN_DEFINITIONS, HANDLERS


def doc(path: str, **extra: Any) -> dict[str, Any]:
    return {'_id': path, 'path': path, **extra}


def test_suites_of_groups_release_files_by_suite() -> None:
    docs = [
        doc('dists/trixie/InRelease'),
        doc('dists/trixie/Release'),
        doc('dists/trixie/Release.gpg'),
        doc('dists/bookworm/mongodb-org/8.0/InRelease'),
        doc('dists/trixie/main/binary-amd64/Release'),  # an index, not a suite's release
        doc('dists/trixie/main/binary-amd64/Packages.xz'),
    ]
    grouped = apt.suites_of(docs, flat=False)
    assert set(grouped) == {'dists/trixie', 'dists/bookworm/mongodb-org/8.0'}
    assert set(grouped['dists/trixie']) == {'InRelease', 'Release', 'Release.gpg'}
    flat = apt.suites_of([doc('Release'), doc('sub/InRelease'), doc('sub/Packages')], flat=True)
    assert set(flat) == {'', 'sub'}


def test_packages_lists_prefers_xz() -> None:
    text = (
        'SHA256:\n'
        f' {"a" * 64} 10 main/binary-amd64/Packages\n'
        f' {"b" * 64} 5 main/binary-amd64/Packages.gz\n'
        f' {"c" * 64} 4 main/binary-amd64/Packages.xz\n'
        f' {"d" * 64} 4 main/binary-arm64/Packages.gz\n'
        f' {"e" * 64} 4 main/i18n/Translation-en.xz\n'
    )
    release = deb822.parse_release(text.encode())
    groups = apt.packages_lists(release, 'dists/trixie')
    assert set(groups) == {('main', 'amd64'), ('main', 'arm64')}
    assert [rel for rel, _ in groups[('main', 'amd64')]] == [
        'main/binary-amd64/Packages.xz',
        'main/binary-amd64/Packages.gz',
        'main/binary-amd64/Packages',
    ]
    assert groups[('main', 'arm64')][0][1].suite == 'trixie'


def test_binary_arch_and_would_list() -> None:
    assert apt.binary_arch('pool/main/h/hello/hello_2.10-5_amd64.deb') == 'amd64'
    assert apt.binary_arch('pool/main/t/tzdata/tzdata_2025a-1_all.deb') == 'all'
    assert apt.binary_arch('pool/main/d/d-i/x_1_arm64.udeb') == 'arm64'
    assert apt.binary_arch('pool/main/h/hello/hello_2.10-5.dsc') is None
    assert apt.binary_arch('pool/main/h/hello/hello.deb') is None
    assert apt.binary_arch('pool/main/h/hello/hello_1_.deb') is None
    assert apt.binary_arch('dists/x/main/binary-amd64/a_1_amd64.deb') is None
    pending = [('pool/main/h/hello/hello_1_amd64.deb', 'amd64'), ('pool/updates/main/t/tz/tz_1_all.deb', 'all')]
    assert apt.would_list(('main', 'amd64'), pending)
    assert not apt.would_list(('main', 'arm64'), pending)
    assert not apt.would_list(('contrib', 'amd64'), pending)
    assert apt.would_list(('updates/main', 'arm64'), pending)  # `all` is in every architecture's index
    assert not apt.would_list(('', ''), pending)


def test_result_summary_and_dict() -> None:
    result = apt.RefreshResult(repo_id='r1', name='debian', suites=2, verified=1, errors=['x'])
    assert '2 suites' in result.summary() and '1 errors' in result.summary()
    assert result.as_dict()['errors'] == ['x']
    skipped = apt.RefreshResult(repo_id='r2', skipped='not an apt proxy repository')
    assert skipped.summary() == 'r2: skipped (not an apt proxy repository)'
    assert skipped.as_dict()['skipped'] == 'not an apt proxy repository'


def test_job_is_registered_and_scheduled() -> None:
    assert AptRefresh().dedupe_key() == 'apt.refresh:*'
    assert AptRefresh(repo_id='r1').dedupe_key() == 'apt.refresh:r1'
    assert AptRefresh.queue == 'prefetch'
    assert 'apt.refresh' in HANDLERS
    assert any(d.id == 'apt.refresh' and d.kind == 'apt.refresh' for d in BUILTIN_DEFINITIONS)


class FakeValkey:
    def __init__(self, value: str | None, *, fail: bool = False) -> None:
        self.value = value
        self.fail = fail

    async def hget(self, key: str, field: str) -> str | None:
        if self.fail:
            raise ConnectionError('down')
        assert key == event_keys.upstream_health_key('r1')
        assert field == 'blocked_until'
        return self.value


async def test_blocked_reads_the_breaker_state() -> None:
    assert not await upstream.blocked(None, 'r1')
    assert await upstream.blocked(FakeValkey('2000'), 'r1', now_ms=1000)  # type: ignore[arg-type]
    assert not await upstream.blocked(FakeValkey('500'), 'r1', now_ms=1000)  # type: ignore[arg-type]
    assert not await upstream.blocked(FakeValkey(None), 'r1', now_ms=1000)  # type: ignore[arg-type]
    assert not await upstream.blocked(FakeValkey('junk'), 'r1', now_ms=1000)  # type: ignore[arg-type]
    assert not await upstream.blocked(FakeValkey('1', fail=True), 'r1')  # type: ignore[arg-type]


def repo_doc(url: str, **proxy: Any) -> dict[str, Any]:
    return {'_id': 'r1', 'name': 'debian', 'format': 'apt', 'type': 'proxy', 'proxy': {'upstream_url': url, **proxy}}


def test_upstream_of_and_url_for() -> None:
    up = upstream.Upstream.of(repo_doc('https://deb.example.com/debian', allow_private_upstream=True))
    assert up.url_for('dists/trixie/InRelease') == 'https://deb.example.com/debian/dists/trixie/InRelease'
    assert up.allow_private and up.auth_kind == 'none'


def test_proxy_for(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, http_proxy='http://proxy.example.com:3128', no_proxy='internal.example.com')
    fetcher = upstream.Fetcher(settings, None)
    plain = upstream.Upstream.of(repo_doc('https://deb.example.com/'))
    assert fetcher.proxy_for('https://deb.example.com/x', plain) == 'http://proxy.example.com:3128'
    assert fetcher.proxy_for('https://a.internal.example.com/x', plain) is None
    own = upstream.Upstream.of(repo_doc('https://deb.example.com/', http_proxy='http://other.example.com:8080'))
    assert fetcher.proxy_for('https://deb.example.com/x', own) == 'http://other.example.com:8080'
    assert upstream.Fetcher(None, None).proxy_for('https://deb.example.com/x', plain) is None


@pytest.fixture
def keyring() -> Keyring:
    return Keyring.from_strings([TEST_KEY])


async def test_credentials_only_go_to_the_upstream_origin(tmp_path: Path, keyring: Keyring) -> None:
    settings = make_settings(tmp_path, ssrf_allow_cidrs=['127.0.0.0/8'])
    async with FakeUpstream() as origin, FakeUpstream() as cdn:
        cdn.routes['/file'] = Respond(200, {}, b'payload')
        origin.routes['/dists/x/InRelease'] = Respond(302, {'Location': f'{cdn.url}file'}, b'')
        secret = keyring.encrypt_str('s3cret', aad=upstream.secret_aad('r1'))
        doc_ = repo_doc(origin.url, auth={'kind': 'basic', 'username': 'u', 'secret_enc': secret})
        async with (
            upstream.Fetcher(settings, keyring) as fetcher,
            fetcher.get(upstream.Upstream.of(doc_), 'dists/x/InRelease') as response,
        ):
            assert response.status_code == 200
            assert await upstream.read_bounded(response, 100) == b'payload'
        expected = 'Basic ' + base64.b64encode(b'u:s3cret').decode()
        assert origin.seen[0].headers.get('authorization') == expected
        assert 'authorization' not in cdn.seen[0].headers
        assert origin.seen[0].headers.get('accept-encoding') == 'identity'


async def test_bearer_and_undecryptable_credentials(tmp_path: Path, keyring: Keyring) -> None:
    settings = make_settings(tmp_path, ssrf_allow_cidrs=['127.0.0.0/8'])
    async with FakeUpstream() as origin:
        origin.routes['/a'] = Respond(200, {}, b'ok')
        token = keyring.encrypt_str('tok', aad=upstream.secret_aad('r1'))
        bearer = upstream.Upstream.of(repo_doc(origin.url, auth={'kind': 'bearer', 'secret_enc': token}))
        async with upstream.Fetcher(settings, keyring) as fetcher:
            async with fetcher.get(bearer, 'a') as response:
                assert response.status_code == 200
            assert origin.seen[-1].headers.get('authorization') == 'Bearer tok'
            wrong = upstream.Upstream.of(repo_doc(origin.url, auth={'kind': 'bearer', 'secret_enc': 'garbage'}))
            with pytest.raises(UpstreamError, match='cannot be decrypted'):
                async with fetcher.get(wrong, 'a'):
                    pass
        async with upstream.Fetcher(settings, None) as no_keys:
            with pytest.raises(UpstreamError, match='SECRET_KEYS'):
                async with no_keys.get(bearer, 'a'):
                    pass


async def test_redirect_limits_ssrf_and_bounds(tmp_path: Path) -> None:
    async with FakeUpstream() as origin:

        def loop(seen: Seen) -> Respond:
            return Respond(302, {'Location': seen.path}, b'')

        origin.routes['/loop'] = loop
        origin.routes['/ftp'] = Respond(302, {'Location': 'ftp://deb.example.com/x'}, b'')
        origin.routes['/big'] = Respond(200, {}, b'x' * 1000)
        origin.routes['/chunked'] = Respond(200, {}, b'x' * 1000, content_length=False)
        origin.routes['/gz'] = Respond(200, {'Content-Encoding': 'gzip'}, b'x')
        up = upstream.Upstream.of(repo_doc(origin.url))
        allowed = make_settings(tmp_path, ssrf_allow_cidrs=['127.0.0.0/8'])
        async with upstream.Fetcher(allowed, None) as fetcher:
            with pytest.raises(UpstreamError, match='too many'):
                async with fetcher.get(up, 'loop'):
                    pass
            with pytest.raises(UpstreamError, match='unsupported'):
                async with fetcher.get(up, 'ftp'):
                    pass
            for path in ('big', 'chunked'):
                async with fetcher.get(up, path) as response:
                    with pytest.raises(PayloadTooLarge):
                        await upstream.read_bounded(response, 100)
            async with fetcher.get(up, 'gz') as response:
                with pytest.raises(UpstreamError, match='gzip'):
                    await upstream.read_bounded(response, 100)
        async with upstream.Fetcher(make_settings(tmp_path), None) as guarded:  # loopback is private
            with pytest.raises(UpstreamError):
                async with guarded.get(up, 'big'):
                    pass


def test_age_of_a_cached_file() -> None:
    now = datetime(2026, 11, 21, 12, tzinfo=UTC)
    assert apt._age_s({'upstream': {'fetched_at': datetime(2026, 11, 21, 11, tzinfo=UTC)}}, now) == 3600
    assert apt._age_s({'upstream': {'fetched_at': datetime(2026, 11, 21, 11)}}, now) == 3600  # naive = UTC
    assert apt._age_s({}, now) == float('inf')
