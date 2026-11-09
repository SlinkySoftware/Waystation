"""Webhook signatures, filters, payloads and the retry schedule (``waystation_common.webhooks``, T-O05)."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest

from waystation_common import webhooks as wh

SECRET = 'whsec-test-0123456789abcdef'
BODY = b'{"id":"01JC0000000000000000000000","type":"content.upload"}'
T = 1_762_700_000


# ---------------------------------------------------------------------------- signatures
def test_signature_is_hmac_sha256_over_timestamp_dot_body() -> None:
    expected = hmac.new(SECRET.encode(), f'{T}.'.encode() + BODY, hashlib.sha256).hexdigest()
    assert wh.sign(SECRET, BODY, T) == f't={T},v1={expected}'


def test_verify_accepts_a_fresh_signature() -> None:
    header = wh.sign(SECRET, BODY, T)
    assert wh.verify(SECRET, BODY, header, now=T + 10)
    assert wh.verify(SECRET, BODY, f'{header},v1=deadbeef', now=T)  # any one v1 may match (secret rotation)


@pytest.mark.parametrize(
    ('secret', 'body', 'now'),
    [
        ('another-secret-0123456789', BODY, T),  # wrong key
        (SECRET, BODY + b' ', T),  # body changed
        (SECRET, BODY, T + 301),  # replayed after the window
        (SECRET, BODY, T - 301),  # from the future
    ],
)
def test_verify_refuses(secret: str, body: bytes, now: int) -> None:
    assert not wh.verify(secret, body, wh.sign(SECRET, BODY, T), now=now)


@pytest.mark.parametrize('header', ['', 'v1=abc', f't={T}', 't=abc,v1=abc', f't={T},v2=abc'])
def test_verify_refuses_malformed_headers(header: str) -> None:
    assert not wh.verify(SECRET, BODY, header, now=T)


def test_headers_carry_ids_signature_and_safe_custom_headers() -> None:
    headers = wh.headers_for(
        body=BODY,
        secret=SECRET,
        event='content.upload',
        event_id='ev1',
        delivery_id='d1',
        attempt=2,
        timestamp=T,
        custom={'Authorization': 'Bearer x', 'X-Team': 'ops', 'Host': 'evil.example.com', 'Waystation-Event': 'x'},
    )
    assert headers['Waystation-Signature'] == wh.sign(SECRET, BODY, T)
    assert headers['Waystation-Event'] == 'content.upload'
    assert headers['Waystation-Event-Id'] == 'ev1'
    assert headers['Waystation-Delivery'] == headers['Idempotency-Key'] == 'd1'
    assert headers['Waystation-Attempt'] == '2'
    assert headers['Waystation-Timestamp'] == str(T)
    assert headers['Authorization'] == 'Bearer x'
    assert headers['X-Team'] == 'ops'
    assert 'Host' not in headers
    assert headers['Content-Type'] == 'application/json'


# ---------------------------------------------------------------------------- filters
@pytest.mark.parametrize(
    ('patterns', 'event', 'expected'),
    [
        (['*'], 'content.upload', True),
        (['content.*'], 'content.delete', True),
        (['content.*'], 'repository.create', False),
        (['repository.delete'], 'repository.delete', True),
        (['repository.delete'], 'repository.update', False),
        (['content.upload'], 'webhook.ping', True),  # a test always goes
        ([], 'content.upload', False),
    ],
)
def test_event_filter(patterns: list[str], event: str, expected: bool) -> None:
    assert wh.event_matches(patterns, event) is expected


@pytest.mark.parametrize(
    ('globs', 'repo', 'expected'),
    [
        ([], 'anything', True),
        ([], None, True),
        (['docker-*'], 'docker-hosted', True),
        (['docker-*'], 'Docker-Hosted', True),
        (['DOCKER-*'], 'docker-hosted', True),
        (['docker-*'], 'npm-hosted', False),
        (['pypi-?'], 'pypi-a', True),
        (['pypi-?'], 'pypi-ab', False),
        (['a', 'b*'], 'bee', True),
        (['docker-*'], None, False),
    ],
)
def test_repository_scope(globs: list[str], repo: str | None, expected: bool) -> None:
    assert wh.repo_matches(globs, repo) is expected


def test_wants_combines_both() -> None:
    hook = {'events': ['content.*'], 'repositories': ['raw-*']}
    assert wh.wants(hook, 'content.upload', 'raw-hosted')
    assert not wh.wants(hook, 'content.upload', 'pypi-hosted')
    assert not wh.wants(hook, 'repository.create', 'raw-hosted')


def test_event_repository() -> None:
    assert wh.event_repository('content.upload', {'kind': 'raw_file'}, {'repository': 'raw-hosted'}) == 'raw-hosted'
    assert wh.event_repository('repository.create', {'kind': 'repository', 'name': 'npm'}, {}) == 'npm'
    assert wh.event_repository('repository.create', {'kind': 'user', 'name': 'npm'}, None) is None
    assert wh.event_repository('content.delete', None, None) is None


# ---------------------------------------------------------------------------- validation
@pytest.mark.parametrize(
    ('url', 'ok'),
    [
        ('https://hooks.example.com/waystation', True),
        ('http://192.0.2.10:8080/in', True),
        ('ftp://hooks.example.com/', False),
        ('https:///path', False),
        ('https://user:pass@hooks.example.com/', False),
        ('https://hooks.example.com/#frag', False),
        ('https://hooks.example.com:99999/', False),
        ('not a url', False),
    ],
)
def test_check_url(url: str, ok: bool) -> None:
    assert (wh.check_url(url) is None) is ok


def test_literal_ip() -> None:
    assert str(wh.literal_ip('http://192.0.2.1/x')) == '192.0.2.1'
    assert str(wh.literal_ip('http://[2001:db8::1]:8080/')) == '2001:db8::1'
    assert wh.literal_ip('https://hooks.example.com/') is None


@pytest.mark.parametrize(
    ('name', 'value', 'ok'),
    [
        ('Authorization', 'Bearer abc', True),
        ('X-Custom', 'v', True),
        ('Content-Type', 'text/plain', False),
        ('host', 'x', False),
        ('Waystation-Signature', 'x', False),
        ('Idempotency-Key', 'x', False),
        ('Bad Name', 'x', False),
        ('X-Inject', 'a\r\nB: c', False),
        ('X-Long', 'x' * 1025, False),
    ],
)
def test_check_header(name: str, value: str, ok: bool) -> None:
    assert (wh.check_header(name, value) is None) is ok


# ---------------------------------------------------------------------------- payloads
def test_build_event_document() -> None:
    doc = wh.build_event(
        event_id='ev1',
        event='content.upload',
        occurred_at=datetime(2026, 11, 10, 8, 0, tzinfo=UTC),
        repository='raw-hosted',
        actor={'kind': 'user', 'name': 'alice', 'id': 'u1', 'token_id': 't1'},
        target={'kind': 'raw_file', 'id': 'a1', 'name': 'raw-hosted/x.bin'},
        details={'repository': 'raw-hosted', 'size': 3},
        changes=[],
        source='https://registry.example.com',
    )
    assert doc == {
        'id': 'ev1',
        'type': 'content.upload',
        'occurred_at': '2026-11-10T08:00:00Z',
        'repository': 'raw-hosted',
        'actor': {'kind': 'user', 'name': 'alice'},
        'target': {'kind': 'raw_file', 'id': 'a1', 'name': 'raw-hosted/x.bin'},
        'details': {'repository': 'raw-hosted', 'size': 3},
        'source': 'https://registry.example.com',
    }
    assert json.loads(wh.encode(doc)) == doc


def test_encode_drops_details_of_an_oversized_document() -> None:
    doc = {'id': 'e', 'type': 'content.upload', 'details': {'blob': 'x' * (wh.MAX_BODY_BYTES + 1)}, 'changes': [1]}
    out = json.loads(wh.encode(doc))
    assert out == {'id': 'e', 'type': 'content.upload', 'details': {'truncated': True}}


# ---------------------------------------------------------------------------- retries and answers
def test_retry_schedule_doubles_up_to_an_hour() -> None:
    assert [wh.retry_delay_s(n, jitter=0) for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert wh.retry_delay_s(8, jitter=0) == 3600
    assert wh.retry_delay_s(500, jitter=0) == 3600
    assert 27 <= wh.retry_delay_s(1) <= 33


@pytest.mark.parametrize(
    ('code', 'ok'), [(200, True), (204, True), (299, True), (301, False), (500, False), (None, False)]
)
def test_success(code: int | None, ok: bool) -> None:
    assert wh.success(code) is ok


def test_snippet_is_bounded_printable_text() -> None:
    assert wh.snippet(b'ok') == 'ok'
    assert len(wh.snippet(b'a' * 5000)) == wh.RESPONSE_SNIPPET_BYTES
    assert wh.snippet(b'line\n\x1b[31mred') == 'line\n�[31mred'
    assert wh.snippet(b'\xff') == '�'
