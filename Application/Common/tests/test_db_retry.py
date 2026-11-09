from collections.abc import Awaitable, Callable

import pytest
from pymongo.errors import AutoReconnect, OperationFailure

from waystation_common.db import retry


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    pauses: list[float] = []

    async def sleep(delay: float) -> None:
        pauses.append(delay)

    monkeypatch.setattr(retry.asyncio, 'sleep', sleep)
    return pauses


def flaky(failures: list[BaseException]) -> tuple[list[int], Callable[[int], Awaitable[int]]]:
    calls: list[int] = []

    @retry.read_retry
    async def read(value: int) -> int:
        calls.append(value)
        if failures:
            raise failures.pop(0)
        return value * 2

    return calls, read


async def test_transient_autoreconnect_is_retried_once_after_a_jittered_pause(no_pause: list[float]) -> None:
    calls, read = flaky([AutoReconnect('mongodb:27017: Connection is closed')])
    assert await read(21) == 42
    assert calls == [21, 21]
    assert len(no_pause) == 1
    assert retry.RETRY_DELAY_S[0] <= no_pause[0] <= retry.RETRY_DELAY_S[1]


async def test_second_failure_propagates() -> None:
    calls, read = flaky([AutoReconnect('one'), AutoReconnect('two')])
    with pytest.raises(AutoReconnect, match='two'):
        await read(1)
    assert len(calls) == 2


async def test_other_errors_are_not_retried(no_pause: list[float]) -> None:
    calls, read = flaky([OperationFailure('bad query')])
    with pytest.raises(OperationFailure):
        await read(1)
    assert len(calls) == 1
    assert no_pause == []


async def test_success_needs_one_call() -> None:
    calls, read = flaky([])
    assert await read(3) == 6
    assert calls == [3]
