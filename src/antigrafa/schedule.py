"""When a recurring job runs: a cron expression, a fixed interval, or times of the day.

Three forms (one per job, see ``[job.<name>]`` in ``antigrafa.example.toml``):

- ``schedule = "0 2 * * *"``: cron, 5 fields (minute hour day-of-month month day-of-week),
  with ``*``, lists ``1,15``, ranges ``1-5``, steps ``*/6`` or ``8-18/2``, names
  (``jan``-``dec``, ``sun``-``sat``; 0 and 7 are Sunday) and ``@hourly``, ``@daily``,
  ``@weekly``, ``@monthly``, ``@yearly``. When both the day of the month and the day of
  the week are restricted, either one matches (as in cron).
- ``every = "6h"``: an interval (``m``, ``h`` or ``d``) from the start of the previous run;
  a job that never ran is due at once.
- ``at = "02:00"`` (or a list of times) with ``days = ["mon", "fri"]`` (every day
  without it).

Times are those of this machine's time zone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # datetime.weekday() order
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
CRON_DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")  # cron numbering: 0 = Sunday
MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
UNITS = {"m": 60, "min": 60, "h": 3600, "d": 86400}
SEARCH_YEARS = 5  # a cron expression that matches nothing within that is refused (31 feb)


def _local(moment: datetime) -> datetime:
    """Wall-clock time of this machine, without the zone (aware datetimes are converted)."""
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment


def _aware(wall: datetime) -> datetime:
    return wall.astimezone()  # a naive datetime is taken as local time


class Schedule:
    text: str

    def next_run(self, after: datetime, last_start: datetime | None = None) -> datetime:
        """The first run strictly after ``after`` (``every``: from ``last_start``)."""
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------- cron


def _cron_value(text: str, names: tuple[str, ...] | None, offset: int) -> int:
    if names and text.lower() in names:
        return names.index(text.lower()) + offset
    if not text.isdigit():
        raise ValueError(f"{text!r} is not a number{' or a name' if names else ''}")
    return int(text)


def _cron_field(text: str, low: int, high: int, names: tuple[str, ...] | None = None, offset: int = 0) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise ValueError(f"step {step_text!r} must be a number of 1 or more")
            step = int(step_text)
        if part == "*":
            first, last = low, high
        elif "-" in part:
            first_text, last_text = part.split("-", 1)
            first, last = _cron_value(first_text, names, offset), _cron_value(last_text, names, offset)
        else:
            first = _cron_value(part, names, offset)
            last = high if step > 1 else first  # "5/15": from 5, every 15
        if not (low <= first <= high and low <= last <= high):
            raise ValueError(f"{part!r} is outside {low}-{high}")
        if first > last:
            raise ValueError(f"range {part!r} goes backwards")
        values.update(range(first, last + 1, step))
    return values


@dataclass
class Cron(Schedule):
    text: str
    minutes: set[int]
    hours: set[int]
    days: set[int]
    months: set[int]
    weekdays: set[int]  # datetime.weekday(): 0 = Monday
    any_day: bool  # day of the month is "*"
    any_weekday: bool

    @classmethod
    def parse(cls, text: str) -> "Cron":
        expanded = MACROS.get(text.strip().lower(), text)
        fields = expanded.split()
        if len(fields) != 5:
            raise ValueError(
                f"cron expression {text!r} needs 5 fields (minute hour day month weekday), or @daily, @hourly…"
            )
        try:
            cron_weekdays = _cron_field(fields[4], 0, 7, CRON_DAYS)
            cron = cls(
                text=text.strip(),
                minutes=_cron_field(fields[0], 0, 59),
                hours=_cron_field(fields[1], 0, 23),
                days=_cron_field(fields[2], 1, 31),
                months=_cron_field(fields[3], 1, 12, MONTHS, 1),
                weekdays={(d - 1) % 7 for d in cron_weekdays},  # cron 0/7 = Sunday = weekday 6
                any_day=fields[2].startswith("*"),
                any_weekday=fields[4].startswith("*"),
            )
        except ValueError as exc:
            raise ValueError(f"cron expression {text!r}: {exc}") from None
        if cron._search(datetime(2000, 1, 1)) is None:
            raise ValueError(f"cron expression {text!r} never matches")
        return cron

    def _day_matches(self, day: date) -> bool:
        in_month = day.day in self.days
        in_week = day.weekday() in self.weekdays
        if self.any_day or self.any_weekday:
            return in_month and in_week
        return in_month or in_week  # both restricted: either one (cron)

    def _search(self, after: datetime) -> datetime | None:
        moment = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = moment + timedelta(days=366 * SEARCH_YEARS)
        while moment < limit:
            if moment.month not in self.months:
                year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
                moment = datetime(year, month, 1)
            elif not self._day_matches(moment.date()):
                moment = datetime.combine(moment.date() + timedelta(days=1), time())
            elif moment.hour not in self.hours:
                moment = moment.replace(minute=0) + timedelta(hours=1)
            elif moment.minute not in self.minutes:
                moment += timedelta(minutes=1)
            else:
                return moment
        return None

    def next_run(self, after: datetime, last_start: datetime | None = None) -> datetime:
        found = self._search(_local(after))
        assert found is not None  # checked by parse()  # nosec B101
        return _aware(found)

    def describe(self) -> str:
        return f"cron {self.text}"


# ---------------------------------------------------------------------- every


def parse_interval(text: str) -> timedelta:
    match = re.fullmatch(r"\s*(\d+)\s*(min|m|h|d)\s*", str(text).lower())
    if not match or int(match.group(1)) < 1:
        raise ValueError(f"every = {text!r}: write a number and m, h or d (30m, 6h, 1d)")
    return timedelta(seconds=int(match.group(1)) * UNITS[match.group(2)])


@dataclass
class Every(Schedule):
    text: str
    interval: timedelta

    def next_run(self, after: datetime, last_start: datetime | None = None) -> datetime:
        if last_start is None:
            return after  # never ran: due at once
        return last_start + self.interval

    def describe(self) -> str:
        return f"every {self.text.strip()}"


# ---------------------------------------------------------------------- at


def parse_time(text: str) -> time:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(text))
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ValueError(f"at = {text!r}: write a time of the day as HH:MM (02:00, 18:30)")
    return time(int(match.group(1)), int(match.group(2)))


@dataclass
class At(Schedule):
    text: str
    times: list[time]
    weekdays: set[int]  # datetime.weekday(): 0 = Monday

    def next_run(self, after: datetime, last_start: datetime | None = None) -> datetime:
        wall = _local(after)
        for offset in range(8):
            day = wall.date() + timedelta(days=offset)
            if day.weekday() not in self.weekdays:
                continue
            for at in self.times:
                candidate = datetime.combine(day, at)
                if candidate > wall:
                    return _aware(candidate)
        raise AssertionError("no day of the week")  # pragma: no cover - days is never empty

    def describe(self) -> str:
        times = ", ".join(t.strftime("%H:%M") for t in self.times)
        if len(self.weekdays) == 7:
            return f"every day at {times}"
        return f"at {times} on {', '.join(WEEKDAYS[d] for d in sorted(self.weekdays))}"


def parse_schedule(schedule: str = "", every: str = "", at: str | list[str] = "", days: list[str] | None = None) -> Schedule:
    """The schedule of a job from its keys: exactly one of ``schedule``, ``every`` and ``at``."""
    given = [name for name, value in (("schedule", schedule), ("every", every), ("at", at)) if value]
    if len(given) != 1:
        raise ValueError("set one of schedule (cron), every or at" if not given
                         else f"set only one of {', '.join(given)}")
    if days and not at:
        raise ValueError("days only goes with at")
    if schedule:
        if not isinstance(schedule, str):
            raise ValueError("schedule must be a cron expression (text)")
        return Cron.parse(schedule)
    if every:
        if not isinstance(every, str):
            raise ValueError("every must be text: 30m, 6h, 1d")
        return Every(every, parse_interval(every))
    texts = [at] if isinstance(at, str) else at
    if not isinstance(texts, list) or not texts or not all(isinstance(t, str) for t in texts):
        raise ValueError("at must be a time (\"02:00\") or a list of times")
    times = sorted({parse_time(t) for t in texts})
    if days is None or days == []:
        weekdays = set(range(7))
    else:
        if not isinstance(days, list) or not all(isinstance(d, str) for d in days):
            raise ValueError(f"days must be a list of {', '.join(WEEKDAYS)}")
        unknown = [d for d in days if d.lower() not in WEEKDAYS]
        if unknown:
            raise ValueError(f"days: unknown {', '.join(unknown)} (use {', '.join(WEEKDAYS)})")
        weekdays = {WEEKDAYS.index(d.lower()) for d in days}
    return At(", ".join(texts), times, weekdays)
