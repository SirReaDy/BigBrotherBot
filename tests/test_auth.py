"""Two-phase auth state machine: resolves-eventually, gives-up, and cancel-on-quit."""

from __future__ import annotations

import asyncio

import pytest

from b3.parsers.cod.auth import AuthInfo, AuthManager

GUID = "G" * 32


async def _nosleep(_seconds: float) -> None:
    """Skip real delays so the state machine runs instantly in tests."""
    return None


@pytest.mark.asyncio
async def test_resolves_after_a_few_polls():
    calls = {"n": 0}

    def resolve(cid: str) -> AuthInfo | None:
        calls["n"] += 1
        if calls["n"] < 3:
            return None  # not in status yet
        return AuthInfo(cid=cid, guid=GUID, pbid="PB", ip="192.0.2.4")

    authed: list[tuple[AuthInfo, int]] = []
    mgr = AuthManager(resolve, lambda info, attempt: authed.append((info, attempt)), sleep=_nosleep)

    mgr.schedule("4")
    await mgr.wait_all()

    assert len(authed) == 1
    info, attempt = authed[0]
    assert attempt == 3  # resolved on the third poll
    assert info.ip == "192.0.2.4"
    assert mgr.pending == set()


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts():
    calls = {"n": 0}

    def resolve(_cid: str) -> AuthInfo | None:
        calls["n"] += 1
        return None  # never resolves

    authed: list = []
    mgr = AuthManager(
        resolve, lambda info, attempt: authed.append(1), max_attempts=3, sleep=_nosleep
    )

    mgr.schedule("4")
    await mgr.wait_all()

    assert authed == []  # never authed
    assert calls["n"] == 3  # tried exactly max_attempts times
    assert mgr.pending == set()


@pytest.mark.asyncio
async def test_cancel_aborts_pending_auth():
    authed: list = []

    async def slow_sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    mgr = AuthManager(
        lambda c: None,
        lambda info, attempt: authed.append(1),
        initial_delay=5.0,
        sleep=slow_sleep,
    )

    mgr.schedule("4")
    mgr.cancel("4")  # player quit before auth fired
    await mgr.wait_all()

    assert authed == []
    assert mgr.pending == set()
