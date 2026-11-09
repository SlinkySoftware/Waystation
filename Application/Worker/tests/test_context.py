"""``JobContext`` live messages (T-O04): progress and log lines reach the Tasks page right away; routine jobs stay
quiet. No MongoDB/Valkey: the run bookkeeping fails quietly here, which a job must survive anyway."""

from typing import Any

from waystation_common.events import EventType, JobLiveMessage
from waystation_common.jobs import BlobGc, JobEnvelope, StatsRollup
from waystation_worker.jobs.context import JobContext


class Recorder:
    def __init__(self) -> None:
        self.messages: list[JobLiveMessage] = []

    def job_live(self, message: JobLiveMessage) -> None:
        self.messages.append(message)


def context(envelope: JobEnvelope, emitter: Any) -> JobContext:
    return JobContext(
        job=envelope,
        run_id='run-1',
        attempt=1,
        worker_id='w-1',
        db=None,  # type: ignore[arg-type]
        valkey=None,  # type: ignore[arg-type]
        clock=lambda: 1_762_682_400.0,
        emitter=emitter,
    )


async def test_progress_and_log_are_published() -> None:
    emitter = Recorder()
    ctx = context(JobEnvelope.for_payload(BlobGc()), emitter)
    await ctx.progress(3, 10, 'sweeping')
    await ctx.progress(4, 10, 'sweeping')  # throttled: written (and published) with the next flush
    await ctx.log('freed 2 blobs')
    await ctx.flush_progress()
    kinds = [(m.type, m.progress.done if m.progress else None, m.lines) for m in emitter.messages]
    assert kinds == [
        (EventType.JOB_PROGRESS, 3, []),
        (EventType.JOB_LOG, None, ['2025-11-09T10:00:00+00:00 freed 2 blobs']),
        (EventType.JOB_PROGRESS, 4, []),
    ]
    assert {(m.run_id, m.kind, m.queue) for m in emitter.messages} == {('run-1', 'blob.gc', 'maintenance')}


async def test_routine_jobs_and_no_emitter_stay_quiet() -> None:
    emitter = Recorder()
    await context(JobEnvelope.for_payload(StatsRollup()), emitter).progress(1, 1)
    assert emitter.messages == []
    await context(JobEnvelope.for_payload(BlobGc()), None).log('nobody listens')  # no emitter: nothing to do
