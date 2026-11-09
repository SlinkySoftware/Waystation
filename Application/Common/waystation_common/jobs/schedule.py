"""Job schedules (``job_definitions.schedule``): 5-field cron expressions in UTC, cron macros, or fixed intervals.

* ``'*/5 * * * *'``: minute, hour, day of month, month, day of week (0-7, 0 and 7 = Sunday). Fields accept ``*``,
  numbers, ranges ``a-b``, steps ``*/n`` / ``a-b/n`` / ``a/n``, comma lists, and month/day names (``jan``, ``mon``).
  When both day of month and day of week are restricted, a day matching either fires (classic cron).
* ``@hourly``, ``@daily`` / ``@midnight``, ``@weekly``, ``@monthly``, ``@yearly`` / ``@annually``.
* ``@every 10s`` / ``@every 5m`` / ``@every 1h``: fires at multiples of the interval since the Unix epoch, so every
  worker computes the same fire times (sub-minute jobs such as the stats rollup).

``parse_schedule(text).next_after(t)`` returns the first fire time strictly after ``t`` (epoch seconds).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

_MACROS = {
    '@yearly': '0 0 1 1 *',
    '@annually': '0 0 1 1 *',
    '@monthly': '0 0 1 * *',
    '@weekly': '0 0 * * 0',
    '@daily': '0 0 * * *',
    '@midnight': '0 0 * * *',
    '@hourly': '0 * * * *',
}
_MONTH_NAMES = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')
_MONTHS = {m: i for i, m in enumerate(_MONTH_NAMES, start=1)}
_DAYS = {d: i for i, d in enumerate(['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'])}
_EVERY_RE = re.compile(r'^@every\s+(\d+)\s*([smh])$')
_UNIT_S = {'s': 1, 'm': 60, 'h': 3600}
_SEARCH_YEARS = 5


class Schedule(Protocol):
    def next_after(self, t: float) -> float: ...


@dataclass(frozen=True)
class Every:
    seconds: int

    def next_after(self, t: float) -> float:
        return float((math.floor(t / self.seconds) + 1) * self.seconds)


def _value(token: str, names: dict[str, int]) -> int:
    token = token.lower()
    if token in names:
        return names[token]
    if not token.isdigit():
        raise ValueError(f'invalid cron value {token!r}')
    return int(token)


def _field(text: str, lo: int, hi: int, names: dict[str, int]) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(','):
        base, _, step_text = part.partition('/')
        step = int(step_text) if step_text.isdigit() else 0 if step_text else 1
        if step < 1:
            raise ValueError(f'invalid cron step in {part!r}')
        if base == '*':
            start, end = lo, hi
        elif '-' in base:
            first, last = base.split('-', 1)
            start, end = _value(first, names), _value(last, names)
        else:
            start = _value(base, names)
            end = hi if step_text else start
        if not (lo <= start <= hi and lo <= end <= hi) or start > end:
            raise ValueError(f'cron value out of range in {part!r} ({lo}-{hi})')
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    days_any: bool
    weekdays_any: bool

    @classmethod
    def parse(cls, text: str) -> Cron:
        fields = text.split()
        if len(fields) != 5:
            raise ValueError('a cron expression has 5 fields: minute hour day-of-month month day-of-week')
        minute, hour, dom, month, dow = fields
        weekdays = frozenset(d % 7 for d in _field(dow, 0, 7, _DAYS))
        return cls(
            minutes=tuple(sorted(_field(minute, 0, 59, {}))),
            hours=tuple(sorted(_field(hour, 0, 23, {}))),
            days=_field(dom, 1, 31, {}),
            months=_field(month, 1, 12, _MONTHS),
            weekdays=weekdays,
            days_any=dom == '*',
            weekdays_any=dow == '*',
        )

    def _day_matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.days
        dow_ok = (day.weekday() + 1) % 7 in self.weekdays
        if self.days_any or self.weekdays_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def next_after(self, t: float) -> float:
        start = datetime.fromtimestamp(t, UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = start.date()
        limit = day + timedelta(days=366 * _SEARCH_YEARS)
        first = True
        while day < limit:
            if self._day_matches(day):
                for hour in self.hours:
                    if first and hour < start.hour:
                        continue
                    for minute in self.minutes:
                        if first and hour == start.hour and minute < start.minute:
                            continue
                        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC).timestamp()
            day += timedelta(days=1)
            first = False
        raise ValueError('the cron expression never fires')


def parse_schedule(text: str) -> Schedule:
    """Parse a schedule; raises ``ValueError`` with a readable message (used to validate definitions)."""
    text = text.strip()
    if match := _EVERY_RE.match(text):
        seconds = int(match.group(1)) * _UNIT_S[match.group(2)]
        if seconds < 1:
            raise ValueError('an @every interval must be at least 1s')
        return Every(seconds)
    cron = Cron.parse(_MACROS.get(text.lower(), text))
    cron.next_after(0.0)  # rejects expressions that never fire (e.g. 30 February)
    return cron


MIN_EDIT_INTERVAL_S = 10  # the shortest ``@every`` an operator may set (the stats rollup's own interval)


def validate_schedule(text: str) -> str:
    """Normalise and check a schedule an operator typed (Tasks page): whitespace collapsed, macros lower-cased,
    ``@every`` at least ``MIN_EDIT_INTERVAL_S``. Raises ``ValueError`` with a readable message."""
    value = ' '.join(text.split())
    if not value:
        raise ValueError('a schedule is required')
    if value.startswith('@'):
        value = value.lower()
        if value not in _MACROS and not _EVERY_RE.match(value):
            known = ', '.join(sorted({*_MACROS, '@every <n>s|m|h'}))
            raise ValueError(f'unknown schedule {value!r} (use a 5-field cron or one of {known})')
    schedule = parse_schedule(value)
    if isinstance(schedule, Every) and schedule.seconds < MIN_EDIT_INTERVAL_S:
        raise ValueError(f'an @every interval must be at least {MIN_EDIT_INTERVAL_S}s')
    return value
