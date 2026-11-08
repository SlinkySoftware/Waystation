from datetime import UTC, datetime
from typing import ClassVar

import pytest
from pydantic import ValidationError

from waystation_common.jobs import JOB_TYPES, QUEUES, JobEnvelope, JobPayload, Queue, StatsRollup, parse_schedule
from waystation_common.jobs.schedule import Cron, Every


def ts(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime('%Y-%m-%d %H:%M')


class Reindex(JobPayload):
    kind: ClassVar[str] = 'test.unit.reindex'
    queue: ClassVar[Queue] = 'index'
    repo_id: str

    def dedupe_key(self) -> str | None:
        return f'reindex:{self.repo_id}'


class Plain(JobPayload):
    kind: ClassVar[str] = 'test.unit.plain'
    n: int = 0


def test_registry_and_envelope() -> None:
    assert JOB_TYPES['test.unit.reindex'] is Reindex
    assert JOB_TYPES['stats.rollup'] is StatsRollup
    assert QUEUES == ('index', 'maintenance', 'stats', 'prefetch', 'import')
    env = JobEnvelope.for_payload(Reindex(repo_id='r1'))
    assert (env.kind, env.queue, env.dedupe_key, env.attempt) == ('test.unit.reindex', 'index', 'reindex:r1', 0)
    again = JobEnvelope.model_validate_json(env.to_json())
    assert again == env
    assert again.decode() == Reindex(repo_id='r1')
    plain = JobEnvelope.for_payload(Plain(n=2), queue='prefetch', definition_id='d1')
    assert plain.dedupe_key == f'id:{plain.id}'  # never merged with another request
    assert (plain.queue, plain.definition_id) == ('prefetch', 'd1')
    assert JobEnvelope.for_payload(Plain(), dedupe_key='custom').dedupe_key == 'custom'


def test_registry_rejects_conflicts_and_unregistered() -> None:
    with pytest.raises(ValueError, match='already registered'):

        class Other(JobPayload):
            kind: ClassVar[str] = 'test.unit.reindex'

    class Abstract(JobPayload):  # no kind: not registered
        pass

    with pytest.raises(ValueError, match='not a registered job type'):
        JobEnvelope.for_payload(Abstract())
    with pytest.raises(ValidationError):
        Reindex(repo_id='r', extra=1)  # type: ignore[call-arg]
    env = JobEnvelope.for_payload(Plain()).model_copy(update={'kind': 'test.unknown'})
    with pytest.raises(KeyError):
        env.decode()


def test_every() -> None:
    every = parse_schedule('@every 10s')
    assert isinstance(every, Every)
    assert every.next_after(100.0) == 110.0
    assert every.next_after(109.9) == 110.0
    assert parse_schedule('@every 5m').next_after(0) == 300
    assert parse_schedule('@every 2h').next_after(1) == 7200


@pytest.mark.parametrize(
    ('expr', 'after', 'expected'),
    [
        ('* * * * *', '2026-11-08 10:15:30', '2026-11-08 10:16'),
        ('*/15 * * * *', '2026-11-08 10:15:00', '2026-11-08 10:30'),
        ('0 3 * * *', '2026-11-08 10:15', '2026-11-09 03:00'),
        ('30 2 1 * *', '2026-11-08 10:15', '2026-12-01 02:30'),
        ('0 0 * * mon', '2026-11-08 10:15', '2026-11-09 00:00'),  # 2026-11-08 is a Sunday
        ('0 0 * * 7', '2026-11-08 10:15', '2026-11-15 00:00'),
        ('0 12 13 * 5', '2026-11-08 10:15', '2026-11-13 12:00'),  # Friday 13th (dom OR dow)
        ('0 12 1 * 2', '2026-11-08 10:15', '2026-11-10 12:00'),  # Tuesday before the 1st
        ('0 9-17/4 * * 1-5', '2026-11-09 13:00', '2026-11-09 17:00'),
        ('5,10 * * feb *', '2026-11-08 10:15', '2027-02-01 00:05'),
        ('0 0 29 2 *', '2026-11-08 10:15', '2028-02-29 00:00'),
        ('@daily', '2026-11-08 10:15', '2026-11-09 00:00'),
        ('@hourly', '2026-11-08 10:15', '2026-11-08 11:00'),
        ('@weekly', '2026-11-08 10:15', '2026-11-15 00:00'),
        ('@monthly', '2026-11-08 10:15', '2026-12-01 00:00'),
        ('@yearly', '2026-11-08 10:15', '2027-01-01 00:00'),
        ('10/20 * * * *', '2026-11-08 10:15', '2026-11-08 10:30'),
    ],
)
def test_cron_next(expr: str, after: str, expected: str) -> None:
    assert iso(parse_schedule(expr).next_after(ts(after))) == expected


@pytest.mark.parametrize(
    'expr',
    ['', '* * * *', '60 * * * *', '* 24 * * *', '0 0 30 2 *', '*/0 * * * *', '5-1 * * * *', 'x * * * *', '@every 0s'],
)
def test_invalid_schedules(expr: str) -> None:
    with pytest.raises(ValueError):
        parse_schedule(expr)


def test_cron_fields() -> None:
    cron = Cron.parse('0,30 */6 * jan-mar sun')
    assert cron.minutes == (0, 30)
    assert cron.hours == (0, 6, 12, 18)
    assert cron.months == frozenset({1, 2, 3})
    assert cron.weekdays == frozenset({0})
