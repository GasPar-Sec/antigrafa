"""Schedules of the recurring jobs: cron, every, at."""

from datetime import datetime, timedelta

import pytest

from antigrafa.schedule import At, Cron, Every, parse_schedule


def local(*args):
    return datetime(*args).astimezone()  # this machine's zone, as the scheduler uses


def nxt(schedule, *after):
    return schedule.next_run(local(*after)).replace(tzinfo=None)


def test_cron_daily_and_hours():
    daily = parse_schedule(schedule="0 2 * * *")
    assert isinstance(daily, Cron)
    assert nxt(daily, 2026, 11, 22, 1, 59) == datetime(2026, 11, 22, 2, 0)
    assert nxt(daily, 2026, 11, 22, 2, 0) == datetime(2026, 11, 23, 2, 0)  # strictly after
    assert nxt(daily, 2026, 12, 31, 23, 0) == datetime(2027, 1, 1, 2, 0)
    six = parse_schedule(schedule="0 */6 * * *")
    assert nxt(six, 2026, 11, 22, 6, 30) == datetime(2026, 11, 22, 12, 0)
    assert daily.describe() == "cron 0 2 * * *"


def test_cron_lists_ranges_steps_and_names():
    workdays = parse_schedule(schedule="30 1 * * mon-fri")
    # 2026-11-21 is a Saturday: next run on Monday the 23rd
    assert nxt(workdays, 2026, 11, 21, 0, 0) == datetime(2026, 11, 23, 1, 30)
    lists = parse_schedule(schedule="15,45 8-18/5 * jan,jul *")
    assert lists.minutes == {15, 45} and lists.hours == {8, 13, 18} and lists.months == {1, 7}
    assert nxt(lists, 2026, 11, 22, 0, 0) == datetime(2027, 1, 1, 8, 15)
    from_five = parse_schedule(schedule="5/20 * * * *")
    assert from_five.minutes == {5, 25, 45}
    sunday = parse_schedule(schedule="0 0 * * 7")
    assert sunday.weekdays == parse_schedule(schedule="0 0 * * 0").weekdays == {6}
    assert nxt(sunday, 2026, 11, 22, 0, 0) == datetime(2026, 11, 29, 0, 0)  # the 22nd is a Sunday, 00:00 passed


def test_cron_day_of_month_or_day_of_week():
    # Both restricted: either one (cron) — the 1st, or any Friday
    either = parse_schedule(schedule="0 3 1 * fri")
    assert nxt(either, 2026, 11, 22, 0, 0) == datetime(2026, 11, 27, 3, 0)  # Friday the 27th
    assert nxt(either, 2026, 11, 28, 0, 0) == datetime(2026, 12, 1, 3, 0)  # the 1st, a Tuesday
    leap = parse_schedule(schedule="0 0 29 2 *")
    assert nxt(leap, 2026, 3, 1, 0, 0) == datetime(2028, 2, 29, 0, 0)


@pytest.mark.parametrize(("macro", "same"), [("@daily", "0 0 * * *"), ("@hourly", "0 * * * *"),
                                             ("@weekly", "0 0 * * 0"), ("@monthly", "0 0 1 * *")])
def test_cron_macros(macro, same):
    a, b = parse_schedule(schedule=macro), parse_schedule(schedule=same)
    assert nxt(a, 2026, 11, 22, 10, 7) == nxt(b, 2026, 11, 22, 10, 7)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("0 2 * *", "needs 5 fields"),
        ("60 * * * *", "outside 0-59"),
        ("0 24 * * *", "outside 0-23"),
        ("0 0 0 * *", "outside 1-31"),
        ("0 0 * 13 *", "outside 1-12"),
        ("0 0 * * 8", "outside 0-7"),
        ("*/0 * * * *", "step '0'"),
        ("5-1 * * * *", "goes backwards"),
        ("x * * * *", "not a number"),
        ("0 0 * * funday", "not a number or a name"),
        ("0 0 31 2 *", "never matches"),
    ],
)
def test_bad_cron(text, message):
    with pytest.raises(ValueError, match=message):
        parse_schedule(schedule=text)


def test_every_counts_from_the_previous_start():
    every = parse_schedule(every="6h")
    assert isinstance(every, Every) and every.interval == timedelta(hours=6)
    now = local(2026, 11, 22, 10, 0)
    assert every.next_run(now) == now  # never ran: at once
    assert every.next_run(now, local(2026, 11, 22, 7, 0)) == local(2026, 11, 22, 13, 0)
    assert parse_schedule(every="30m").interval == timedelta(minutes=30)
    assert parse_schedule(every="2 d").interval == timedelta(days=2)
    assert parse_schedule(every="45min").describe() == "every 45min"


@pytest.mark.parametrize("text", ["6", "0h", "6w", "h", "-1h", "1.5h"])
def test_bad_interval(text):
    with pytest.raises(ValueError, match="m, h or d"):
        parse_schedule(every=text)


def test_at_times_and_days():
    at = parse_schedule(at="02:00")
    assert isinstance(at, At) and at.describe() == "every day at 02:00"
    assert nxt(at, 2026, 11, 22, 3, 0) == datetime(2026, 11, 23, 2, 0)
    twice = parse_schedule(at=["14:30", "2:00"], days=["mon", "Fri"])
    assert twice.describe() == "at 02:00, 14:30 on mon, fri"
    assert nxt(twice, 2026, 11, 23, 2, 0) == datetime(2026, 11, 23, 14, 30)  # Monday
    assert nxt(twice, 2026, 11, 23, 15, 0) == datetime(2026, 11, 27, 2, 0)  # then Friday
    assert nxt(twice, 2026, 11, 27, 20, 0) == datetime(2026, 11, 30, 2, 0)  # next Monday


@pytest.mark.parametrize(
    ("keys", "message"),
    [
        ({}, "set one of schedule"),
        ({"schedule": "@daily", "every": "1h"}, "only one of schedule, every"),
        ({"every": "1h", "days": ["mon"]}, "days only goes with at"),
        ({"at": "25:00"}, "HH:MM"),
        ({"at": "2h"}, "HH:MM"),
        ({"at": []}, "set one of"),
        ({"at": [2]}, "list of times"),
        ({"at": "02:00", "days": ["monday"]}, "unknown monday"),
        ({"at": "02:00", "days": "mon"}, "list of"),
        ({"schedule": 5}, "cron expression"),
        ({"every": 6}, "must be text"),
    ],
)
def test_bad_schedule_keys(keys, message):
    with pytest.raises(ValueError, match=message):
        parse_schedule(**keys)
