"""Activity filters, time ranges, paging and CSV export (T-U07) against real Valkey."""

import csv
import io
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_testing import add_repo, asgi_client

from waystation_common.events import ActivityEvent, CacheState, EventType, keys
from waystation_common.settings import Settings
from waystation_server.api import live as live_api
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.services import Services
from waystation_server.stats import live

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
T0 = 1_762_680_000_000  # epoch ms


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(bootstrap_admin_password=SecretStr(PASSWORD))


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    await add_repo(services, 'https://example.com/', name='files')
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


async def _add(services: Services, ms: int, **kw: Any) -> None:
    event: dict[str, Any] = {
        'type': EventType.CACHE_HIT,
        'ts': datetime.fromtimestamp(ms / 1000, UTC),
        'repo': 'files',
        'format': 'raw',
        'path': f'dir/{ms}.bin',
        'principal': 'alice',
        'cache': CacheState.HIT,
        'bytes': 100,
    }
    event.update(kw)
    data = ActivityEvent.model_validate(event).model_dump_json()
    await services.valkey.xadd(keys.ACTIVITY, {'data': data}, id=f'{ms}-0')


async def _seed(services: Services) -> None:
    for i in range(10):
        await _add(services, T0 + i * 60_000, principal='alice' if i % 2 else 'bob')
    await _add(services, T0 + 600_000, type=EventType.TRANSFER_FAILED, cache=CacheState.ERROR, detail={'error': 'boom'})
    await _add(services, T0 + 660_000, type=EventType.CACHE_STALE, cache=CacheState.STALE, principal='=cmd')


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


async def test_activity_filters(admin: httpx.AsyncClient, services: Services) -> None:
    await _seed(services)

    async def ids(**params: Any) -> list[str]:
        page = (await admin.get('/api/v1/activity', params={'limit': 100, **params})).json()
        return [e['id'] for e in page['items']]

    assert len(await ids()) == 12
    assert await ids(outcome='failure') == [f'{T0 + 600_000}-0']
    assert await ids(kind='error') == [f'{T0 + 600_000}-0']
    assert await ids(cache='STALE') == [f'{T0 + 660_000}-0']
    assert len(await ids(principal='BOB')) == 5
    assert await ids(format='npm') == []
    assert await ids(repo='nope') == []
    # the time range is inclusive at both ends
    ranged = await ids(since=_iso(T0 + 120_000), until=_iso(T0 + 240_000))
    assert ranged == [f'{T0 + 240_000}-0', f'{T0 + 180_000}-0', f'{T0 + 120_000}-0']
    until = _iso(T0 + 240_000)
    page = (await admin.get('/api/v1/activity', params={'limit': 1, 'until': until})).json()
    assert page['items'][0]['path'] == f'dir/{T0 + 240_000}.bin'
    rest = (
        await admin.get('/api/v1/activity', params={'limit': 100, 'cursor': page['next_cursor'], 'until': until})
    ).json()
    assert [e['id'] for e in rest['items']] == [f'{T0 + i * 60_000}-0' for i in (3, 2, 1, 0)]
    assert rest['next_cursor'] is None

    bad = await admin.get('/api/v1/activity', params={'since': _iso(T0 + 1000), 'until': _iso(T0)})
    assert bad.status_code == 400
    assert (await admin.get('/api/v1/activity', params={'cache': 'NOPE'})).status_code == 422


async def test_sparse_filter_pages_through_history(admin: httpx.AsyncClient, services: Services) -> None:
    await _add(services, T0 - 5, principal='zed')
    for i in range(30):
        await _add(services, T0 + i, principal='bob')
    flt = live.ActivityFilter(visible=frozenset({'files'}), principal='zed')
    items, cursor = await live.activity_page(services.valkey, flt, cursor=None, limit=5, scan_cap=10)
    assert items == []
    assert cursor is not None  # the scan cap was reached: keep paging
    found: list[dict[str, Any]] = []
    while cursor:
        page, cursor = await live.activity_page(services.valkey, flt, cursor=cursor, limit=5, scan_cap=10)
        found.extend(page)
    assert [e['principal'] for e in found] == ['zed']


async def test_export_csv(admin: httpx.AsyncClient, services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed(services)
    response = await admin.get('/api/v1/activity/export', params={'principal': 'bob'})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/csv')
    assert response.headers['content-disposition'].startswith('attachment; filename="waystation-activity-')
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 5
    assert rows[0]['id'] == f'{T0 + 480_000}-0'  # newest first
    assert (rows[0]['repository'], rows[0]['cache'], rows[0]['bytes']) == ('files', 'HIT', '100')

    stale = await admin.get('/api/v1/activity/export', params={'cache': 'STALE'})
    assert next(csv.DictReader(io.StringIO(stale.text)))['principal'] == "'=cmd"  # formula neutralised

    monkeypatch.setattr(live_api, 'EXPORT_MAX_ROWS', 3)
    capped = list(csv.DictReader(io.StringIO((await admin.get('/api/v1/activity/export')).text)))
    assert len(capped) == 3

    empty = await admin.get('/api/v1/activity/export', params={'repo': 'nope'})
    assert empty.text.startswith('time,kind,outcome')
    assert len(empty.text.strip().splitlines()) == 1


async def test_export_needs_permission(client: httpx.AsyncClient) -> None:
    assert (await client.get('/api/v1/activity/export')).status_code == 401
