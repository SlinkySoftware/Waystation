"""Webhooks (T-O05) against real MongoDB/Valkey and a fake receiver: the admin API (write-only secrets, validation,
audit), events from real uploads through the durable outbox to signed deliveries (the worker's handlers), retries
with backoff into a dead delivery, redelivery from the log, the SSRF guard, timeouts and the metrics."""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond, Seen
from ws_testing import asgi_client

from waystation_common import webhooks as wh
from waystation_common.db.collections import AUDIT, JOB_OUTBOX, USERS, WEBHOOK_DELIVERIES, WEBHOOKS
from waystation_common.events import keys as event_keys
from waystation_common.jobs import JobEnvelope, WebhookDeliver, WebhookEvent, WebhookSweep
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.security import passwords
from waystation_server.services import Services
from waystation_worker.jobs import HANDLERS, JobContext

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
SECRET = 'whsec-integration-0123456789'
API = '/api/v1'


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(bootstrap_admin_password=SecretStr(PASSWORD))


async def login(client: httpx.AsyncClient, username: str) -> httpx.AsyncClient:
    response = await client.post(f'{API}/auth/login', json={'username': username, 'password': PASSWORD})
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return client


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        yield await login(client, 'admin')


async def run(services: Services, payload: Any) -> dict[str, Any]:
    """Run a job handler as a worker would."""
    env = JobEnvelope.for_payload(payload)
    ctx = JobContext(
        job=env,
        run_id='run',
        attempt=1,
        worker_id='w',
        db=services.db,
        valkey=services.valkey,
        settings=services.settings,
    )
    result = await HANDLERS[payload.kind](payload, ctx)
    assert result is not None
    return result


async def drain_outbox(services: Services) -> list[dict[str, Any]]:
    """Flush the server's notifier, then run every ``webhook.event`` job in the outbox (the dispatcher's job)."""
    await services.webhooks.flush()
    results = []
    async for doc in services.db[JOB_OUTBOX].find({'job.kind': WebhookEvent.kind, 'done_at': None}):
        payload = JobEnvelope.model_validate(doc['job']).decode()
        results.append(await run(services, payload))
        await services.db[JOB_OUTBOX].update_one({'_id': doc['_id']}, {'$set': {'done_at': datetime.now(UTC)}})
    return results


async def upload_auth(admin: httpx.AsyncClient) -> tuple[str, str]:
    """Basic auth for format clients: the user name and an API token (passwords are refused there)."""
    created = await admin.post(f'{API}/auth/me/tokens', json={'name': 'raw', 'scopes': ['read', 'write']})
    assert created.status_code == 201, created.text
    return 'admin', str(created.json()['secret'])


def hook(url: str, **fields: Any) -> dict[str, Any]:
    return {
        'name': 'ci-hook',
        'url': url,
        'events': ['*'],
        'secret': SECRET,
        'allow_private': True,  # the fake receiver listens on 127.0.0.1
        **fields,
    }


async def make_due(services: Services) -> None:
    past = datetime.now(UTC) - timedelta(seconds=1)
    await services.db[WEBHOOK_DELIVERIES].update_many({'status': 'pending'}, {'$set': {'next_attempt_at': past}})


# ---------------------------------------------------------------------------- definitions
async def test_crud_validation_secrets_and_audit(services: Services, admin: httpx.AsyncClient) -> None:
    url = 'https://hooks.example.com/waystation'
    missing = await admin.post(f'{API}/webhooks', json={**hook(url), 'secret': None})
    assert missing.status_code == 422
    assert missing.json()['errors'][0]['field'] == 'secret'
    private = await admin.post(f'{API}/webhooks', json=hook('http://127.0.0.1:9/in', allow_private=False))
    assert private.status_code == 422 and private.json()['errors'][0]['code'] == 'private_address'
    creds = await admin.post(f'{API}/webhooks', json=hook('https://u:p@hooks.example.com/'))
    assert creds.status_code == 422
    bad_header = await admin.post(f'{API}/webhooks', json=hook(url, headers={'Host': 'x'}))
    assert bad_header.status_code == 422 and bad_header.json()['errors'][0]['field'] == 'headers.Host'

    created = await admin.post(
        f'{API}/webhooks',
        json=hook(url, repositories=['raw-*'], headers={'Authorization': 'Bearer receiver-token-xyz'}),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body['secret_set'] is True and body['header_names'] == ['Authorization']
    assert 'secret' not in body and 'headers' not in body and body['last_delivery'] is None
    webhook_id = body['id']
    assert (await admin.post(f'{API}/webhooks', json=hook(url, name='CI-HOOK'))).status_code == 409

    stored = await services.db[WEBHOOKS].find_one({'_id': webhook_id})
    assert stored is not None
    assert SECRET not in str(stored) and 'receiver-token-xyz' not in str(stored)

    # null secret and headers keep the stored ones
    updated = await admin.put(
        f'{API}/webhooks/{webhook_id}', json={**hook(url, enabled=False), 'secret': None, 'headers': None}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()['enabled'] is False and updated.json()['header_names'] == ['Authorization']
    again = await services.db[WEBHOOKS].find_one({'_id': webhook_id})
    assert again is not None and again['secret_enc'] == stored['secret_enc']

    listed = await admin.get(f'{API}/webhooks')
    assert listed.status_code == 200
    assert [w['name'] for w in listed.json()['items']] == ['ci-hook']
    assert listed.json()['events'] == list(wh.EVENTS)

    await services.db[USERS].insert_one(
        {
            '_id': 'id-rita',
            'username': 'rita',
            'display_name': 'Rita',
            'source': 'local',
            'password_hash': await passwords.hash_password(PASSWORD),
            'roles': ['reader'],
            'created_at': datetime.now(UTC),
        }
    )
    async with asgi_client(services) as plain:
        rita = await login(plain, 'rita')
        assert (await rita.get(f'{API}/webhooks')).status_code == 403
        assert (await rita.get(f'{API}/webhooks/{webhook_id}')).status_code == 403

    assert (await admin.delete(f'{API}/webhooks/{webhook_id}')).status_code == 204
    assert (await admin.get(f'{API}/webhooks/{webhook_id}')).status_code == 404

    await services.audit.flush()
    actions = [d['action'] async for d in services.db[AUDIT].find({'target.kind': 'webhook'}).sort('ts', 1)]
    assert actions == ['webhook.create', 'webhook.update', 'webhook.delete']
    stored_audit = str([d async for d in services.db[AUDIT].find({})])
    assert SECRET not in stored_audit and 'receiver-token-xyz' not in stored_audit


# ---------------------------------------------------------------------------- events end to end
async def test_upload_events_are_delivered_signed(services: Services, admin: httpx.AsyncClient) -> None:
    async with FakeUpstream({'/hook': Respond(200, {}, b'thanks')}) as receiver:
        made = await admin.post(
            f'{API}/webhooks',
            json=hook(
                f'{receiver.url}hook',
                events=['content.upload', 'repository.*'],
                repositories=['raw-*'],
                headers={'X-Team': 'platform'},
            ),
        )
        assert made.status_code == 201, made.text
        webhook_id = made.json()['id']

        repo = await admin.post(
            f'{API}/repositories', json={'name': 'raw-files', 'format': 'raw', 'type': 'hosted', 'online': True}
        )
        assert repo.status_code == 201, repo.text
        other = await admin.post(
            f'{API}/repositories', json={'name': 'npm-files', 'format': 'raw', 'type': 'hosted', 'online': True}
        )
        assert other.status_code == 201, other.text
        auth = await upload_auth(admin)
        put = await admin.put('/repository/raw-files/dist/app.tar.gz', content=b'payload', auth=auth)
        assert put.status_code == 201, put.text

        results = await drain_outbox(services)
        assert sorted(r['event'] for r in results) == ['content.upload', 'repository.create']  # not npm-files
        assert all(r['deliveries'] == 1 and r['succeeded'] == 1 for r in results)
        assert await drain_outbox(services) == []  # nothing left; a re-run creates nothing new

        posts = [s for s in receiver.seen if s.path == '/hook']
        assert len(posts) == 2
        upload = next(s for s in posts if s.headers['waystation-event'] == 'content.upload')
        assert upload.method == 'POST'
        assert upload.headers['content-type'] == 'application/json'
        assert upload.headers['x-team'] == 'platform'
        assert upload.headers['idempotency-key'] == upload.headers['waystation-delivery']
        assert upload.headers['waystation-attempt'] == '1'
        assert wh.verify(SECRET, upload.body, upload.headers['waystation-signature'])
        assert not wh.verify('another-secret-0123456789', upload.body, upload.headers['waystation-signature'])
        document = httpx.Response(200, content=upload.body).json()
        assert document['type'] == 'content.upload'
        assert document['repository'] == 'raw-files'
        assert document['actor'] == {'kind': 'user', 'name': 'admin'}
        assert document['details']['path'] == 'dist/app.tar.gz'
        assert document['source'] == services.settings.base_url
        assert document['id'] == upload.headers['waystation-event-id']

        log = (await admin.get(f'{API}/webhooks/{webhook_id}/deliveries')).json()
        assert [d['status'] for d in log['items']] == ['succeeded', 'succeeded']
        assert all(d['status_code'] == 200 and d['attempts'] == 1 for d in log['items'])
        one = await admin.get(f'{API}/webhooks/{webhook_id}/deliveries/{log["items"][0]["id"]}')
        assert one.status_code == 200
        detail = one.json()
        assert detail['log'][0]['response'] == 'thanks'
        assert detail['request_headers']['X-Team'] == '***'
        assert detail['request_body'].startswith('{')
        listed = (await admin.get(f'{API}/webhooks')).json()['items'][0]
        assert listed['last_delivery']['status'] == 'succeeded'

        # a webhook turned off gets nothing
        await admin.put(f'{API}/webhooks/{webhook_id}', json={**hook(f'{receiver.url}hook'), 'enabled': False})
        assert (await admin.put('/repository/raw-files/dist/two.bin', content=b'2', auth=auth)).status_code == 201
        assert await drain_outbox(services) == []
    totals = await services.valkey.hgetall(event_keys.WEBHOOK_TOTALS)
    assert totals.get('succeeded') == '2'


# ---------------------------------------------------------------------------- retries, dead, redelivery
async def test_retries_back_off_then_dead_and_redeliver(services: Services, admin: httpx.AsyncClient) -> None:
    state = {'status': 500}

    def answer(seen: Seen) -> Respond:
        return Respond(state['status'], {}, b'receiver is down' if state['status'] >= 500 else b'ok')

    async with FakeUpstream({'/hook': answer}) as receiver:
        made = await admin.post(f'{API}/webhooks', json=hook(f'{receiver.url}hook', max_attempts=3))
        webhook_id = made.json()['id']
        repo = await admin.post(
            f'{API}/repositories', json={'name': 'raw-files', 'format': 'raw', 'type': 'hosted', 'online': True}
        )
        assert repo.status_code == 201

        [result] = await drain_outbox(services)
        assert result == {'event': 'repository.create', 'deliveries': 1, 'succeeded': 0}
        [pending] = (await admin.get(f'{API}/webhooks/{webhook_id}/deliveries')).json()['items']
        assert pending['status'] == 'pending' and pending['attempts'] == 1 and pending['status_code'] == 500
        wait = datetime.fromisoformat(pending['next_attempt_at']) - datetime.now(UTC)
        assert timedelta(seconds=20) < wait < timedelta(seconds=40)  # 30 s ± 10 %

        assert await run(services, WebhookSweep()) == {'attempted': 0, 'succeeded': 0}  # not due yet
        for _ in range(2):
            await make_due(services)
            assert (await run(services, WebhookSweep()))['attempted'] == 1
        detail = (await admin.get(f'{API}/webhooks/{webhook_id}/deliveries/{pending["id"]}')).json()
        assert detail['status'] == 'dead' and detail['attempts'] == 3 and detail['next_attempt_at'] is None
        assert [a['n'] for a in detail['log']] == [3, 2, 1]
        assert detail['log'][0]['response'] == 'receiver is down'
        assert [s.headers['waystation-attempt'] for s in receiver.seen] == ['1', '2', '3']
        assert len({s.headers['waystation-delivery'] for s in receiver.seen}) == 1
        await make_due(services)
        assert (await run(services, WebhookSweep()))['attempted'] == 0  # dead stays dead

        dead_only = await admin.get(f'{API}/webhooks/{webhook_id}/deliveries', params={'status': 'dead'})
        assert [d['id'] for d in dead_only.json()['items']] == [pending['id']]

        state['status'] = 204
        again = await admin.post(f'{API}/webhooks/{webhook_id}/deliveries/{pending["id"]}/redeliver')
        assert again.status_code == 202
        new_id = again.json()['delivery_id']
        assert new_id != pending['id']
        assert (await run(services, WebhookDeliver(delivery_id=new_id)))['status'] == 'succeeded'
        last = receiver.seen[-1]
        assert last.headers['waystation-delivery'] == new_id
        assert last.headers['waystation-event-id'] == receiver.seen[0].headers['waystation-event-id']
        assert last.body == receiver.seen[0].body
        redelivered = (await admin.get(f'{API}/webhooks/{webhook_id}/deliveries/{new_id}')).json()
        assert redelivered['trigger'] == 'redeliver' and redelivered['redelivery_of'] == pending['id']
        # the delivery job running twice does not send twice
        assert (await run(services, WebhookDeliver(delivery_id=new_id)))['status'] == 'not due'

    totals = await services.valkey.hgetall(event_keys.WEBHOOK_TOTALS)
    assert totals == {'failed': '2', 'dead': '1', 'succeeded': '1'}
    metrics = (await admin.get('/metrics')).text
    assert 'waystation_webhook_deliveries_total{outcome="dead"} 1.0' in metrics
    assert 'waystation_webhook_deliveries_pending 0.0' in metrics

    await services.audit.flush()
    assert await services.db[AUDIT].count_documents({'action': 'webhook.redeliver'}) == 1


# ---------------------------------------------------------------------------- test event, SSRF, timeouts
async def test_test_event_ssrf_refusal_and_timeout(services: Services, admin: httpx.AsyncClient) -> None:
    async with FakeUpstream({'/hook': Respond(200), '/slow': Respond(hang=True)}) as receiver:
        # A name resolving to loopback, private targets not allowed: refused at connect time, never retried.
        name_url = f'http://localhost:{receiver.port}/hook'
        made = await admin.post(f'{API}/webhooks', json=hook(name_url, allow_private=False))
        assert made.status_code == 201, made.text
        webhook_id = made.json()['id']
        sent = await admin.post(f'{API}/webhooks/{webhook_id}/test')
        assert sent.status_code == 202
        delivery_id = sent.json()['delivery_id']
        assert (await run(services, WebhookDeliver(delivery_id=delivery_id)))['status'] == 'dead'
        refused = (await admin.get(f'{API}/webhooks/{webhook_id}/deliveries/{delivery_id}')).json()
        assert refused['event'] == 'webhook.ping' and refused['trigger'] == 'test'
        assert 'private or reserved' in refused['error']
        assert receiver.seen == []

        # Allowed: the ping arrives, signed.
        await admin.put(f'{API}/webhooks/{webhook_id}', json={**hook(name_url), 'secret': None})
        delivery_id = (await admin.post(f'{API}/webhooks/{webhook_id}/test')).json()['delivery_id']
        assert (await run(services, WebhookDeliver(delivery_id=delivery_id)))['status'] == 'succeeded'
        [ping] = receiver.seen
        assert ping.headers['waystation-event'] == 'webhook.ping'
        assert wh.verify(SECRET, ping.body, ping.headers['waystation-signature'])

        # A receiver that never answers: the timeout ends the attempt (tests are not retried).
        slow = await admin.post(
            f'{API}/webhooks', json=hook(f'{receiver.url}slow', name='slow-hook', timeout_s=1, max_attempts=5)
        )
        slow_id = slow.json()['id']
        delivery_id = (await admin.post(f'{API}/webhooks/{slow_id}/test')).json()['delivery_id']
        assert (await run(services, WebhookDeliver(delivery_id=delivery_id)))['status'] == 'dead'
        timed_out = (await admin.get(f'{API}/webhooks/{slow_id}/deliveries/{delivery_id}')).json()
        assert timed_out['error'] == 'timed out after 1 s'
        assert timed_out['log'][0]['latency_ms'] >= 900

    await services.audit.flush()
    assert await services.db[AUDIT].count_documents({'action': 'webhook.test'}) == 3
    # deleting a webhook drops its log
    assert (await admin.delete(f'{API}/webhooks/{slow_id}')).status_code == 204
    assert await services.db[WEBHOOK_DELIVERIES].count_documents({'webhook_id': slow_id}) == 0
