"""Cleanup planning (T-O01): version ordering per format, coordinates from asset paths, criteria, docker cascade."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from waystation_common.cleanup import AssetRow, Criteria, ManifestRow, RepoData, TagRow, coordinates, plan_repository
from waystation_common.cleanup.model import check_schedule
from waystation_common.cleanup.versions import (
    dpkg_compare,
    is_prerelease,
    natural_key,
    rpm_compare,
    rpmvercmp,
    sort_key,
)

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


def asset(
    path: str, *, age: float = 400, downloaded: float | None = None, size: int = 100, kind: str = 'content'
) -> AssetRow:
    return AssetRow(
        id=f'a:{path}',
        path=path,
        kind=kind,
        size=size,
        blob_sha256=f'{abs(hash(path)):064x}'[:64],
        created_at=days_ago(age),
        last_downloaded_at=days_ago(downloaded) if downloaded is not None else None,
    )


def plan(fmt: str, criteria: Criteria, assets: list[AssetRow]) -> list[str]:
    return [c.path for c in plan_repository(RepoData(format=fmt, assets=assets), criteria, NOW)]


# ---------------------------------------------------------------- versions
@pytest.mark.parametrize(
    ('lower', 'higher'),
    [
        ('1.0.0-alpha', '1.0.0-alpha.1'),
        ('1.0.0-alpha.1', '1.0.0-alpha.beta'),
        ('1.0.0-alpha.beta', '1.0.0-beta'),
        ('1.0.0-beta.2', '1.0.0-beta.11'),
        ('1.0.0-rc.1', '1.0.0'),
        ('1.9.0', '1.10.0'),
        ('not-semver', '0.0.1'),
    ],
)
def test_semver_precedence(lower: str, higher: str) -> None:
    assert sort_key('npm', lower) < sort_key('npm', higher)


@pytest.mark.parametrize(
    ('lower', 'higher'),
    [
        ('1.0~rc1-1', '1.0-1'),
        ('1.0-1', '1.0-1+b1'),
        ('1.0-1', '1:0.1-1'),
        ('2.9', '2.10'),
        ('1.2.3a', '1.2.3b'),
        ('1.0', '1.0-1'),
        ('1.0~', '1.0'),
        ('1.0+dfsg', '1.0.1'),
    ],
)
def test_dpkg_order(lower: str, higher: str) -> None:
    assert dpkg_compare(lower, higher) < 0 < dpkg_compare(higher, lower)
    assert sort_key('apt', lower) < sort_key('apt', higher)


def test_dpkg_equal() -> None:
    assert dpkg_compare('1.0-1', '1.0-1') == 0
    assert dpkg_compare('0:1.0-1', '1.0-1') == 0


@pytest.mark.parametrize(
    ('lower', 'higher'),
    [
        ('1.0', '1.0.1'),
        ('1.0a', '1.0.1'),
        ('1.0~rc1', '1.0'),
        ('1.0', '1.0^git1'),
        ('1.0^git1', '1.0.1'),
        ('2.0', '10.0'),
        ('1.a', '1.1'),
        ('1.0', '1.0a'),
        ('1.0~rc1', '1.0~rc2'),
    ],
)
def test_rpmvercmp(lower: str, higher: str) -> None:
    assert rpmvercmp(lower, higher) == -1 and rpmvercmp(higher, lower) == 1


def test_rpm_evr() -> None:
    assert rpmvercmp('1.0', '1.0') == 0 and rpmvercmp('1.0', '1_0') == 0
    assert rpm_compare('1.0-2.el9', '1.0-10.el9') < 0
    assert rpm_compare('1:1.0-1', '2.0-1') > 0
    assert sort_key('yum', '1.20.1-14.el9') > sort_key('yum', '1.20.1-2.el9')


def test_prerelease_and_generic_order() -> None:
    assert is_prerelease('npm', '2.0.0-rc.1') and not is_prerelease('npm', '2.0.0')
    assert is_prerelease('apt', '1.0~beta1-1') and not is_prerelease('apt', '1.0-1')
    assert is_prerelease('docker', 'nightly') and is_prerelease('docker', '3.1-rc2')
    assert not is_prerelease('docker', 'latest') and not is_prerelease('docker', 'bookworm-slim')
    assert natural_key('v2.10') > natural_key('v2.9')


# ---------------------------------------------------------------- coordinates and criteria
def test_coordinates() -> None:
    scoped = coordinates('npm', '@types/node/-/node-22.5.0.tgz')
    assert (scoped.name, scoped.version, scoped.group) == ('@types/node', '22.5.0', '@types/node')
    assert coordinates('npm', 'left-pad/-/left-pad-1.3.0.tgz').version == '1.3.0'
    deb = coordinates('apt', 'debian/pool/main/n/nginx/nginx_1.22.1-9%3a1_amd64.deb')
    assert (deb.name, deb.version, deb.group) == ('nginx', '1.22.1-9:1', 'nginx/amd64')
    rpm = coordinates('yum', 'BaseOS/x86_64/os/Packages/n/nginx-core-1.20.1-14.el9.x86_64.rpm')
    assert (rpm.name, rpm.version, rpm.group) == ('nginx-core', '1.20.1-14.el9', 'nginx-core.x86_64')
    raw = coordinates('raw', 'builds/app/app-1.2.zip')
    assert (raw.name, raw.version, raw.group, raw.ordered_by_version) == (
        'builds/app/app-1.2.zip',
        'app-1.2.zip',
        'builds/app',
        False,
    )


def test_criteria_validation() -> None:
    with pytest.raises(ValidationError, match='regular expression'):
        Criteria(name_include='(')
    with pytest.raises(ValidationError):
        Criteria(keep_latest_n_versions=0)
    with pytest.raises(ValidationError):
        Criteria.model_validate({'unknown': 1})
    assert Criteria(name_include='').name_include is None
    assert not Criteria(name_exclude='x').selects_anything
    assert Criteria(untagged_older_than_days=3).selects_anything
    assert check_schedule('  0  3 * * * ') == '0 3 * * *' and check_schedule('') is None
    with pytest.raises(ValueError, match='cron'):
        check_schedule('every day')


NPM = [
    asset('left-pad/-/left-pad-1.0.0.tgz', age=500, downloaded=300),
    asset('left-pad/-/left-pad-1.1.0.tgz', age=400),
    asset('left-pad/-/left-pad-1.10.0.tgz', age=10, downloaded=1),
    asset('left-pad/-/left-pad-2.0.0-rc.1.tgz', age=5),
    asset('is-odd/-/is-odd-3.0.1.tgz', age=200, downloaded=150),
    asset('left-pad/-/packument.json', age=500, kind='metadata'),
]


def test_last_downloaded_falls_back_to_cached_time() -> None:
    assert plan('npm', Criteria(last_downloaded_older_than_days=100), NPM) == [
        'is-odd/-/is-odd-3.0.1.tgz',
        'left-pad/-/left-pad-1.0.0.tgz',
        'left-pad/-/left-pad-1.1.0.tgz',
    ]


def test_keep_latest_uses_version_order() -> None:
    # newest two by semver: 2.0.0-rc.1 and 1.10.0 (not by cache time or string order)
    assert plan('npm', Criteria(keep_latest_n_versions=2), NPM) == [
        'left-pad/-/left-pad-1.0.0.tgz',
        'left-pad/-/left-pad-1.1.0.tgz',
    ]


def test_criteria_are_anded_and_exclusions_protect() -> None:
    criteria = Criteria(published_older_than_days=100, name_include='^left', version_exclude=r'^1\.0\.')
    assert plan('npm', criteria, NPM) == ['left-pad/-/left-pad-1.1.0.tgz']
    assert plan('npm', Criteria(prerelease_only=True), NPM) == ['left-pad/-/left-pad-2.0.0-rc.1.tgz']
    assert plan('npm', Criteria(name_exclude='.'), NPM) == []  # protective only: selects nothing
    reasons = plan_repository(RepoData('npm', NPM), Criteria(published_older_than_days=300), NOW)
    assert reasons[0].reason == 'published 500 days ago'


def test_apt_keeps_latest_per_name_and_arch() -> None:
    debs = [
        asset('pool/main/c/curl/curl_7.88.1-10_amd64.deb'),
        asset('pool/main/c/curl/curl_7.88.1-10+deb12u5_amd64.deb'),
        asset('pool/main/c/curl/curl_7.88.1-10_arm64.deb'),
        asset('pool/main/c/curl/curl_7.88.1-10.dsc'),
    ]
    assert plan('apt', Criteria(keep_latest_n_versions=1), debs) == ['pool/main/c/curl/curl_7.88.1-10_amd64.deb']


def test_raw_keeps_latest_per_folder_by_cache_time() -> None:
    files = [asset('builds/a.zip', age=3), asset('builds/b.zip', age=2), asset('builds/c.zip', age=1)]
    assert plan('raw', Criteria(keep_latest_n_versions=2), files) == ['builds/a.zip']


# ---------------------------------------------------------------- docker
L1, L2, L3, CFG = 'sha256:' + '1' * 64, 'sha256:' + '2' * 64, 'sha256:' + '3' * 64, 'sha256:' + 'c' * 64
IDX, M_AMD, M_ARM, M_OLD, SIG = ('sha256:' + c * 64 for c in 'abdef')


def docker_data() -> RepoData:
    manifests = [
        ManifestRow('library/nginx', IDX, days_ago(5), children=(M_AMD, M_ARM)),
        ManifestRow('library/nginx', M_AMD, days_ago(5), blobs=(L1, L2, CFG)),
        ManifestRow('library/nginx', M_ARM, days_ago(5), blobs=(L1, L3)),
        ManifestRow('library/nginx', SIG, days_ago(5), subject=IDX),
        ManifestRow('library/nginx', M_OLD, days_ago(90), blobs=(L1,)),
    ]
    tags = [
        TagRow('library/nginx', '1.27', IDX, days_ago(30)),
        TagRow('library/nginx', 'latest', M_AMD, days_ago(1)),
    ]
    assets = [
        asset('library/nginx/manifests/1.27', age=30),
        asset('library/nginx/manifests/latest', age=1, downloaded=0.5),
        *(asset(f'library/nginx/manifests/{d}', age=5) for d in (IDX, M_AMD, M_ARM, SIG)),
        asset(f'library/nginx/manifests/{M_OLD}', age=90),
        *(asset(f'blobs/{d}', size=1000) for d in (L1, L2, L3, CFG)),
    ]
    return RepoData('docker', assets=assets, tags=tags, manifests=manifests)


def kinds(criteria: Criteria) -> list[tuple[str, str | None]]:
    return [(c.kind, c.version) for c in plan_repository(docker_data(), criteria, NOW)]


def test_docker_removing_a_tag_cascades_to_what_only_it_reached() -> None:
    # removing 1.27 orphans the index, its arm64 child and the signature; amd64 stays (latest), so do L1/L2/CFG
    assert kinds(Criteria(version_include='^1\\.27$')) == [
        ('tag', '1.27'),
        ('manifest', IDX),
        ('manifest', M_ARM),
        ('manifest', SIG),
        ('layer', L3),
    ]


def test_docker_untagged_and_keep_latest() -> None:
    assert kinds(Criteria(untagged_older_than_days=30)) == [('manifest', M_OLD)]  # L1 still used
    assert kinds(Criteria(untagged_older_than_days=100)) == []
    removed = kinds(Criteria(keep_latest_n_versions=1))
    assert removed[0] == ('tag', '1.27')
    assert kinds(Criteria(last_downloaded_older_than_days=2))[0] == ('tag', '1.27')


def test_docker_everything_goes_when_every_tag_goes() -> None:
    plan_ = plan_repository(docker_data(), Criteria(published_older_than_days=1, untagged_older_than_days=1), NOW)
    assert {c.kind for c in plan_} == {'tag', 'manifest', 'layer'}
    assert len([c for c in plan_ if c.kind == 'layer']) == 4
    assert len([c for c in plan_ if c.kind == 'manifest']) == 5
    assert sum(c.size for c in plan_ if c.kind == 'layer') == 4000


# ---------------------------------------------------------------- pypi (T-P5-PYPI)
PEP440_ORDER = [
    'not-a-version',
    '1.0.dev0',
    '1.0a1.dev1',
    '1.0a1',
    '1.0a2',
    '1.0b1',
    '1.0rc1',
    '1.0',
    '1.0+abc',
    '1.0+abc.5',
    '1.0+5',
    '1.0.post1.dev1',
    '1.0.post1',
    '1.0.1',
    '1.1.dev1',
    '1.10',
    '2!0.1',
]


def test_pep440_order_matches_tuple_and_string_keys() -> None:
    from waystation_common.cleanup.versions import pep440_key, pep440_sort_string

    shuffled = PEP440_ORDER[5:] + PEP440_ORDER[:5]
    assert sorted(shuffled, key=pep440_key) == PEP440_ORDER
    assert sorted(shuffled, key=pep440_sort_string) == PEP440_ORDER
    assert pep440_key('1.0') == pep440_key('1.0.0') and pep440_sort_string('1.0') == pep440_sort_string('1.0.0')
    assert pep440_key('1.0-1') == pep440_key('1.0.post1') and pep440_key('1.0RC1') == pep440_key('1.0rc1')
    assert is_prerelease('pypi', '2.0rc1') and is_prerelease('pypi', '2.0.dev3') and not is_prerelease('pypi', '2.0')
    assert not is_prerelease('pypi', '2.0.post1')


def test_pypi_coordinates_and_versions_keep_all_files_of_a_release() -> None:
    whl = coordinates('pypi', 'packages/six/six-1.17.0-py2.py3-none-any.whl')
    assert (whl.name, whl.version, whl.group, whl.ordered_by_version) == ('six', '1.17.0', 'six', True)
    assert coordinates('pypi', 'packages/six/six-1.17.0-py2.py3-none-any.whl.metadata').version == '1.17.0'
    assert coordinates('pypi', 'packages/ws-demo/ws_demo-0.1.0.tar.gz').version == '0.1.0'
    files = [
        asset('packages/six/six-1.16.0.tar.gz'),
        asset('packages/six/six-1.16.0-py2.py3-none-any.whl'),
        asset('packages/six/six-1.17.0.tar.gz'),
        asset('packages/six/six-1.17.0-py2.py3-none-any.whl'),
        asset('packages/six/six-1.17.0-py2.py3-none-any.whl.metadata'),
    ]
    assert plan('pypi', Criteria(keep_latest_n_versions=1), files) == [
        'packages/six/six-1.16.0-py2.py3-none-any.whl',
        'packages/six/six-1.16.0.tar.gz',
    ]
