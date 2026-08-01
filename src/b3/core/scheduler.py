"""Cron scheduler — timed work for the core and for plugins.

The classic bot had ``b3/cron.py``: a ``CronTab`` with crontab-style fields, an ``OneTimeCronTab``,
a ``PluginCronTab`` that only fired while its plugin was enabled, and a dedicated thread waking every
second. Plugins depended on it heavily (``scheduler``, ``pingwatch``, ``afk``, stats rollups), so
without it none of them can be ported.

This is the same contract on ``asyncio``: the field syntax is preserved (``*``, ``N``, ``*/N``,
``a-b``, ``a-b/N`` and comma-separated combinations, with day-of-week 0 = Monday as in the legacy
``tm_wday``), and so is the plugin gate. What changes:

* No thread. :meth:`Scheduler.tick` is a plain coroutine that runs whatever is due *now*; the caller
  owns the loop. That makes the whole thing testable against a :class:`b3.core.clock.FakeClock`
  instead of by sleeping.
* Fields are parsed and range-checked once, at registration, so a typo in a plugin's schedule fails
  immediately with a clear message rather than silently never firing.
* Times are evaluated in the configured ``bot.time_zone`` (the legacy code used local time and left
  the setting unused).
* A handler that raises is logged and skipped — one bad plugin must not stop every other schedule.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

from b3.core.clock import Clock, SystemClock

log = logging.getLogger(__name__)

# Inclusive (min, max) for each field. day/month start at 1, everything else at 0.
FIELD_RANGES: dict[str, tuple[int, int]] = {
    "second": (0, 59),
    "minute": (0, 59),
    "hour": (0, 23),
    "day": (1, 31),
    "month": (1, 12),
    "dow": (0, 6),  # 0 = Monday, as in the legacy tm_wday
}


class CronError(ValueError):
    """A malformed schedule field. Raised at registration, not at fire time."""


def parse_field(value: str | int, name: str) -> frozenset[int] | None:
    """Parse one crontab field. Returns None for ``*`` (matches everything).

    Supports the legacy syntax: ``*``, ``N``, ``*/N``, ``a-b``, ``a-b/N``, and comma-separated
    combinations of those.
    """
    low, high = FIELD_RANGES[name]
    text = str(value).strip()
    if not text:
        raise CronError(f"{name}: empty schedule field")
    if text == "*":
        return None

    values: set[int] = set()
    for fragment in text.split(","):
        values.update(_parse_fragment(fragment.strip(), name, low, high))
    return frozenset(values)


def _parse_fragment(fragment: str, name: str, low: int, high: int) -> Iterable[int]:
    if not fragment:
        raise CronError(f"{name}: empty fragment in schedule")

    body, _, step_text = fragment.partition("/")
    step = 1
    if step_text:
        if not step_text.isdigit() or int(step_text) < 1:
            raise CronError(f"{name}: {fragment!r} has an invalid step")
        step = int(step_text)

    if body == "*":
        return range(low, high + 1, step)

    if "-" in body:
        start_text, _, end_text = body.partition("-")
        start, end = _as_int(start_text, fragment, name), _as_int(end_text, fragment, name)
        if start > end:
            raise CronError(f"{name}: {fragment!r} has a reversed range")
        _check_range(start, fragment, name, low, high)
        _check_range(end, fragment, name, low, high)
        return range(start, end + 1, step)

    single = _as_int(body, fragment, name)
    _check_range(single, fragment, name, low, high)
    if step_text:
        return range(single, high + 1, step)
    return (single,)


def _as_int(text: str, fragment: str, name: str) -> int:
    if not text.strip().isdigit():
        raise CronError(f"{name}: {fragment!r} is not a valid schedule fragment")
    return int(text)


def _check_range(value: int, fragment: str, name: str, low: int, high: int) -> None:
    if not low <= value <= high:
        raise CronError(f"{name}: {fragment!r} is outside the allowed range {low}-{high}")


@dataclass(frozen=True, slots=True)
class CronSpec:
    """A parsed schedule. ``None`` in a field means "every"."""

    second: frozenset[int] | None = frozenset({0})
    minute: frozenset[int] | None = None
    hour: frozenset[int] | None = None
    day: frozenset[int] | None = None
    month: frozenset[int] | None = None
    dow: frozenset[int] | None = None

    @classmethod
    def parse(
        cls,
        *,
        second: str | int = 0,
        minute: str | int = "*",
        hour: str | int = "*",
        day: str | int = "*",
        month: str | int = "*",
        dow: str | int = "*",
    ) -> CronSpec:
        """Build a spec from crontab-style fields, validating each one."""
        return cls(
            second=parse_field(second, "second"),
            minute=parse_field(minute, "minute"),
            hour=parse_field(hour, "hour"),
            day=parse_field(day, "day"),
            month=parse_field(month, "month"),
            dow=parse_field(dow, "dow"),
        )

    def matches(self, when: datetime) -> bool:
        return (
            (self.second is None or when.second in self.second)
            and (self.minute is None or when.minute in self.minute)
            and (self.hour is None or when.hour in self.hour)
            and (self.day is None or when.day in self.day)
            and (self.month is None or when.month in self.month)
            and (self.dow is None or when.weekday() in self.dow)
        )

    def describe(self) -> str:
        def show(values: frozenset[int] | None) -> str:
            return "*" if values is None else ",".join(str(v) for v in sorted(values))

        return " ".join(
            show(v) for v in (self.second, self.minute, self.hour, self.day, self.month, self.dow)
        )


@dataclass(slots=True)
class ScheduledTask:
    """One registered schedule. ``plugin`` gates firing on that plugin being enabled."""

    name: str
    spec: CronSpec
    handler: Callable[[], Any]
    plugin: object | None = None
    one_shot: bool = False
    cancelled: bool = False
    runs: int = 0
    last_fired: int | None = field(default=None)  # epoch second, for once-per-second dedupe

    def cancel(self) -> None:
        self.cancelled = True

    def is_gated_off(self) -> bool:
        """True when the owning plugin is disabled (the legacy ``PluginCronTab`` behaviour)."""
        if self.plugin is None:
            return False
        is_enabled = getattr(self.plugin, "is_enabled", None)
        return is_enabled is not None and not is_enabled()


class Scheduler:
    """Runs :class:`ScheduledTask`s. The caller drives it via :meth:`tick` or :meth:`run`."""

    def __init__(self, clock: Clock | None = None, tz: tzinfo | None = None) -> None:
        self._clock = clock or SystemClock()
        self._tz = tz
        self._tasks: list[ScheduledTask] = []

    @property
    def tz(self) -> tzinfo | None:
        """The zone schedules are evaluated in. Settable so `!reconfig` can change it."""
        return self._tz

    @tz.setter
    def tz(self, value: tzinfo | None) -> None:
        self._tz = value

    # -- registration ------------------------------------------------------

    def add(
        self,
        handler: Callable[[], Any],
        *,
        second: str | int = 0,
        minute: str | int = "*",
        hour: str | int = "*",
        day: str | int = "*",
        month: str | int = "*",
        dow: str | int = "*",
        name: str | None = None,
        plugin: object | None = None,
        one_shot: bool = False,
    ) -> ScheduledTask:
        """Register a schedule. Raises :class:`CronError` on a malformed field."""
        spec = CronSpec.parse(
            second=second, minute=minute, hour=hour, day=day, month=month, dow=dow
        )
        task = ScheduledTask(
            name=name or str(getattr(handler, "__name__", "task")),
            spec=spec,
            handler=handler,
            plugin=plugin,
            one_shot=one_shot,
        )
        self._tasks.append(task)
        log.debug("scheduled %r [%s]", task.name, spec.describe())
        return task

    def remove(self, task: ScheduledTask) -> None:
        task.cancel()
        if task in self._tasks:
            self._tasks.remove(task)

    def remove_plugin(self, plugin: object) -> int:
        """Drop every schedule owned by a plugin (called when it unloads)."""
        doomed = [t for t in self._tasks if t.plugin is plugin]
        for task in doomed:
            self.remove(task)
        return len(doomed)

    @property
    def tasks(self) -> list[ScheduledTask]:
        return list(self._tasks)

    # -- execution ---------------------------------------------------------

    def due(self, now: int | None = None) -> list[ScheduledTask]:
        """Tasks that should fire at ``now`` (default: the clock's current second)."""
        now = self._clock.epoch() if now is None else now
        when = datetime.fromtimestamp(now, self._tz)
        return [
            task
            for task in self._tasks
            if not task.cancelled
            and not task.is_gated_off()
            and task.last_fired != now  # at most once per second, however often we are ticked
            and task.spec.matches(when)
        ]

    async def tick(self, now: int | None = None) -> int:
        """Run everything due. Returns how many handlers fired."""
        now = self._clock.epoch() if now is None else now
        ran = 0
        for task in self.due(now):
            task.last_fired = now
            task.runs += 1
            ran += 1
            try:
                result = task.handler()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - one bad schedule must not stop the others
                log.exception("scheduled task %r raised", task.name)
            if task.one_shot:
                self.remove(task)
        return ran

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Tick once per second until ``stop`` is set. Used by the live run loop."""
        while stop is None or not stop.is_set():
            await self.tick()
            # Sleep to the next whole second so schedules do not drift.
            now = self._clock.now()
            await asyncio.sleep(max(0.01, 1.0 - (now - int(now))))
