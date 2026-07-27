"""Event bus: ordering, veto, enable-gating, error isolation, sync + async handlers."""

from __future__ import annotations

import asyncio

import pytest

from b3.core.bus import EventBus
from b3.core.events import Event, EventType, VetoEvent


@pytest.mark.asyncio
async def test_dispatch_in_registration_order():
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe(EventType.CLIENT_SAY, lambda e: seen.append("a"))

    async def async_handler(_e: Event) -> None:
        seen.append("b")

    bus.subscribe(EventType.CLIENT_SAY, async_handler)

    ok = await bus.publish(Event(EventType.CLIENT_SAY))
    assert ok is True
    assert seen == ["a", "b"]  # sync + async both ran, in order


@pytest.mark.asyncio
async def test_veto_stops_propagation():
    bus = EventBus()
    seen: list[str] = []

    def veto_handler(_e: Event) -> None:
        raise VetoEvent

    bus.subscribe(EventType.CLIENT_SAY, veto_handler)
    bus.subscribe(EventType.CLIENT_SAY, lambda e: seen.append("after"))

    ok = await bus.publish(Event(EventType.CLIENT_SAY))
    assert ok is False
    assert seen == []  # second handler never ran


@pytest.mark.asyncio
async def test_disabled_subscription_skipped():
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe(EventType.CLIENT_SAY, lambda e: seen.append("x"), is_enabled=lambda: False)
    await bus.publish(Event(EventType.CLIENT_SAY))
    assert seen == []


@pytest.mark.asyncio
async def test_handler_exception_is_isolated():
    bus = EventBus()
    seen: list[str] = []

    def boom(_e: Event) -> None:
        raise RuntimeError("kaboom")

    bus.subscribe(EventType.CLIENT_SAY, boom)
    bus.subscribe(EventType.CLIENT_SAY, lambda e: seen.append("survived"))

    ok = await bus.publish(Event(EventType.CLIENT_SAY))
    assert ok is True  # exception swallowed, propagation continued
    assert seen == ["survived"]


@pytest.mark.asyncio
async def test_no_handlers_is_noop():
    bus = EventBus()
    assert await bus.publish(Event(EventType.GAME_ROUND_START)) is True


def test_unsubscribe():
    bus = EventBus()
    sub = bus.subscribe(EventType.CLIENT_SAY, lambda e: None)
    assert bus.handler_count(EventType.CLIENT_SAY) == 1
    bus.unsubscribe(sub)
    assert bus.handler_count(EventType.CLIENT_SAY) == 0


@pytest.mark.asyncio
async def test_publish_soon_off_the_loop_is_reported_not_swallowed(caplog):
    """The bus belongs to the event loop. A worker thread calling it must get a clear error
    rather than a stray 'coroutine was never awaited' warning at some unrelated moment."""
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(EventType.CLIENT_JOIN, lambda e: seen.append(e))

    result = await asyncio.to_thread(bus.publish_soon, Event(EventType.CLIENT_JOIN))

    assert result is None
    assert seen == []
    assert "off the event loop" in caplog.text
