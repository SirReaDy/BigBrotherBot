"""Cron scheduler: field parsing (legacy syntax), matching, plugin gating, execution."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from b3.core.clock import FakeClock
from b3.core.scheduler import CronError, CronSpec, Scheduler, parse_field

# FakeClock's default start: 2023-11-14 22:13:20 UTC.
BASE = 1_700_000_000


# -- field parsing (the legacy CronTab syntax) -----------------------------


@pytest.mark.parametrize(
    ("value", "name", "expected"),
    [
        ("*", "minute", None),
        ("5", "minute", {5}),
        (5, "minute", {5}),
        ("*/15", "minute", {0, 15, 30, 45}),
        ("*/20", "minute", {0, 20, 40}),
        ("1-5", "hour", {1, 2, 3, 4, 5}),
        ("1-10/3", "hour", {1, 4, 7, 10}),
        ("1,3,5", "hour", {1, 3, 5}),
        ("0,30", "second", {0, 30}),
        ("1-3,20,40-42", "minute", {1, 2, 3, 20, 40, 41, 42}),
        ("*/2", "dow", {0, 2, 4, 6}),
        ("1", "day", {1}),
        ("12", "month", {12}),
    ],
)
def test_parse_field(value, name, expected):
    result = parse_field(value, name)
    assert result == (None if expected is None else frozenset(expected))


@pytest.mark.parametrize(
    ("value", "name", "match"),
    [
        ("60", "minute", "outside the allowed range 0-59"),
        ("24", "hour", "outside the allowed range 0-23"),
        ("7", "dow", "outside the allowed range 0-6"),
        ("0", "day", "outside the allowed range 1-31"),
        ("13", "month", "outside the allowed range 1-12"),
        ("10-5", "minute", "reversed range"),
        ("abc", "minute", "not a valid schedule fragment"),
        ("*/0", "minute", "invalid step"),
        ("*/x", "minute", "invalid step"),
        ("", "minute", "empty schedule field"),
        ("1,,2", "minute", "empty fragment"),
    ],
)
def test_parse_field_rejects_bad_input(value, name, match):
    with pytest.raises(CronError, match=match):
        parse_field(value, name)


def test_bad_field_fails_at_registration_not_at_fire_time():
    scheduler = Scheduler(FakeClock())
    with pytest.raises(CronError):
        scheduler.add(lambda: None, minute="99")
    assert scheduler.tasks == []


# -- matching --------------------------------------------------------------


def test_spec_matches_all_fields():
    spec = CronSpec.parse(second=0, minute=30, hour=12)
    assert spec.matches(datetime(2024, 5, 6, 12, 30, 0)) is True
    assert spec.matches(datetime(2024, 5, 6, 12, 31, 0)) is False
    assert spec.matches(datetime(2024, 5, 6, 13, 30, 0)) is False
    assert spec.matches(datetime(2024, 5, 6, 12, 30, 1)) is False


def test_day_of_week_is_monday_zero_like_the_legacy_tm_wday():
    monday = CronSpec.parse(dow=0)
    assert monday.matches(datetime(2024, 5, 6, 0, 0, 0)) is True  # a Monday
    assert monday.matches(datetime(2024, 5, 7, 0, 0, 0)) is False  # Tuesday

    sunday = CronSpec.parse(dow=6)
    assert sunday.matches(datetime(2024, 5, 12, 0, 0, 0)) is True


def test_default_spec_fires_once_a_minute():
    spec = CronSpec.parse()  # second=0, everything else *
    assert spec.matches(datetime(2024, 1, 1, 3, 4, 0)) is True
    assert spec.matches(datetime(2024, 1, 1, 3, 4, 30)) is False


def test_describe_is_readable():
    assert CronSpec.parse(second=0, minute="*/30").describe() == "0 0,30 * * * *"


# -- execution -------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_runs_a_due_task():
    clock = FakeClock(start=BASE)
    scheduler = Scheduler(clock, tz=timezone.utc)
    calls = []
    scheduler.add(lambda: calls.append("x"), second="*")

    assert await scheduler.tick() == 1
    assert calls == ["x"]


@pytest.mark.asyncio
async def test_a_task_fires_at_most_once_per_second():
    clock = FakeClock(start=BASE)
    scheduler = Scheduler(clock, tz=timezone.utc)
    calls = []
    scheduler.add(lambda: calls.append("x"), second="*")

    await scheduler.tick()
    await scheduler.tick()  # same second: the live loop ticks ~5x/second
    await scheduler.tick()
    assert calls == ["x"]

    clock.advance(1)
    await scheduler.tick()
    assert calls == ["x", "x"]


@pytest.mark.asyncio
async def test_a_task_does_not_fire_when_it_does_not_match():
    clock = FakeClock(start=BASE)  # 22:13:20 UTC
    scheduler = Scheduler(clock, tz=timezone.utc)
    calls = []
    scheduler.add(lambda: calls.append("x"), second=0, minute=0, hour=3)

    assert await scheduler.tick() == 0
    assert calls == []


@pytest.mark.asyncio
async def test_async_handlers_are_awaited():
    clock = FakeClock(start=BASE)
    scheduler = Scheduler(clock, tz=timezone.utc)
    calls = []

    async def handler():
        calls.append("async")

    scheduler.add(handler, second="*")
    await scheduler.tick()
    assert calls == ["async"]


@pytest.mark.asyncio
async def test_one_shot_task_runs_once_and_is_removed():
    clock = FakeClock(start=BASE)
    scheduler = Scheduler(clock, tz=timezone.utc)
    calls = []
    scheduler.add(lambda: calls.append("x"), second="*", one_shot=True)

    await scheduler.tick()
    clock.advance(1)
    await scheduler.tick()

    assert calls == ["x"]
    assert scheduler.tasks == []


@pytest.mark.asyncio
async def test_a_raising_task_does_not_stop_the_others(caplog):
    clock = FakeClock(start=BASE)
    scheduler = Scheduler(clock, tz=timezone.utc)
    calls = []

    def boom():
        raise RuntimeError("plugin bug")

    scheduler.add(boom, second="*", name="boom")
    scheduler.add(lambda: calls.append("survivor"), second="*")

    with caplog.at_level("ERROR"):
        assert await scheduler.tick() == 2
    assert calls == ["survivor"]
    assert "scheduled task 'boom' raised" in caplog.text


@pytest.mark.asyncio
async def test_cancelled_and_removed_tasks_stop_firing():
    clock = FakeClock(start=BASE)
    scheduler = Scheduler(clock, tz=timezone.utc)
    calls = []
    task = scheduler.add(lambda: calls.append("x"), second="*")

    scheduler.remove(task)
    assert await scheduler.tick() == 0
    assert calls == []
    assert scheduler.tasks == []


@pytest.mark.asyncio
async def test_runs_counter_tracks_firings():
    clock = FakeClock(start=BASE)
    scheduler = Scheduler(clock, tz=timezone.utc)
    task = scheduler.add(lambda: None, second="*")
    for _ in range(3):
        await scheduler.tick()
        clock.advance(1)
    assert task.runs == 3


# -- plugin gating (the legacy PluginCronTab) ------------------------------


class FakePlugin:
    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled

    def is_enabled(self) -> bool:
        return self._enabled


@pytest.mark.asyncio
async def test_a_disabled_plugins_schedule_does_not_fire():
    clock = FakeClock(start=BASE)
    scheduler = Scheduler(clock, tz=timezone.utc)
    plugin = FakePlugin(enabled=False)
    calls = []
    scheduler.add(lambda: calls.append("x"), second="*", plugin=plugin)

    assert await scheduler.tick() == 0
    assert calls == []

    plugin._enabled = True
    assert await scheduler.tick() == 1
    assert calls == ["x"]


def test_remove_plugin_drops_every_schedule_it_owns():
    scheduler = Scheduler(FakeClock())
    plugin, other = FakePlugin(), FakePlugin()
    scheduler.add(lambda: None, second="*", plugin=plugin)
    scheduler.add(lambda: None, second="*", plugin=plugin)
    scheduler.add(lambda: None, second="*", plugin=other)

    assert scheduler.remove_plugin(plugin) == 2
    assert len(scheduler.tasks) == 1


# -- time zone -------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedules_use_the_configured_time_zone():
    """BASE is 22:13 UTC, which is 00:13 the next day in Chisinau (UTC+2 in November)."""
    calls = []

    utc = Scheduler(FakeClock(start=BASE), tz=timezone.utc)
    utc.add(lambda: calls.append("utc"), second=20, minute=13, hour=0)
    assert await utc.tick() == 0

    local = Scheduler(FakeClock(start=BASE), tz=ZoneInfo("Europe/Chisinau"))
    local.add(lambda: calls.append("local"), second=20, minute=13, hour=0)
    assert await local.tick() == 1
    assert calls == ["local"]
