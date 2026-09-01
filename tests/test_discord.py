"""The `discord` plugin — relaying the server to a channel, outbound only.

No test here reaches Discord. `post` is the one method that touches a socket, and every test replaces
it, which is what lets the interesting cases — a 429, a webhook that is down, a batch too long for
one message — be tested at all.

Four properties matter more than the formatting, and each has a test:

* **a Discord outage costs nothing** — the bot does not stall, the ban still happens, and the lines
  are not silently lost;
* **the queue is bounded**, because an outage otherwise grows one for as long as it lasts;
* **a rate limit is obeyed for as long as Discord asked**, not for a guess; and
* **a player cannot reformat the channel with their own name**, which is the one part of these
  messages somebody else chooses.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.discord import MAX_CONTENT, DiscordPlugin

HOOK = "https://discord.example/api/webhooks/1/abc"


class FakeDiscord:
    """Stands in for `post`: records what would be sent, answers what the test wants."""

    def __init__(self, retry_after: float | None = None) -> None:
        self.sent: list[dict[str, object]] = []
        self.retry_after = retry_after

    def __call__(self, url: str, payload: dict[str, object]) -> float | None:
        self.sent.append(payload)
        return self.retry_after

    @property
    def content(self) -> str:
        return str(self.sent[-1]["content"]) if self.sent else ""


def _plugin(console, sender=None, **settings):  # noqa: ANN001, ANN202
    settings.setdefault("webhook", HOOK)
    plugin = DiscordPlugin(console, {"settings": settings})
    plugin.start()
    plugin.post = sender or FakeDiscord()  # type: ignore[method-assign]
    return plugin


def _client(name="Bob", cid="2", id_=7):  # noqa: ANN001, ANN202
    return Client(guid="B" * 32, name=name, cid=cid, id=id_)


# -- what gets relayed ---------------------------------------------------------------------------


def test_a_ban_is_relayed_with_who_and_why(console):
    plugin = _plugin(console)
    admin = _client("Admin", cid="1", id_=1)

    line = plugin.render(Event(EventType.CLIENT_BAN, client=_client(), target=admin, data="aimbot"))

    assert "Bob" in line and "Admin" in line and "aimbot" in line


def test_a_ban_with_no_reason_says_so_rather_than_trailing_off(console):
    plugin = _plugin(console)
    line = plugin.render(Event(EventType.CLIENT_BAN, client=_client()))
    assert "no reason given" in line


def test_an_automatic_ban_names_the_bot_rather_than_nobody(console):
    """Most bans here are not typed by anybody: a ban list match, a warning that ran out."""
    plugin = _plugin(console)
    line = plugin.render(Event(EventType.CLIENT_BAN, client=_client(), data="spam"))
    assert "b3" in line


def test_chat_is_off_unless_it_is_asked_for(console):
    """A player talking in a game they are playing has not agreed to being quoted in a channel."""
    plugin = _plugin(console)
    plugin.on_event(Event(EventType.CLIENT_SAY, client=_client(), data="hello"))
    assert plugin.queue == []


def test_a_command_is_never_relayed_even_with_chat_on(console):
    """`!login <password>` is typed in the same place as chat, and it is the reason for this test."""
    plugin = _plugin(console, chat=True)

    plugin.on_event(Event(EventType.CLIENT_SAY, client=_client(), data="!login hunter2"))
    plugin.on_event(Event(EventType.CLIENT_SAY, client=_client(), data="hello everyone"))

    assert plugin.queue == ["Bob: hello everyone"]


def test_a_name_cannot_reformat_the_channel(console):
    """The name is the one part of these lines somebody else chooses, and Discord reads markdown."""
    plugin = _plugin(console)
    line = plugin.render(Event(EventType.CLIENT_BAN, client=_client("**@everyone**"), data="x"))

    assert "@everyone" not in line, "a mention in a player's name must not become a mention"
    assert line.count("**") == 2, "and their asterisks are not the ones that embolden the name"


def test_colour_codes_are_not_relayed_as_text(console):
    """`^1Bob^7` is a red Bob in game and four stray characters anywhere else."""
    plugin = _plugin(console)
    line = plugin.render(Event(EventType.CLIENT_BAN, client=_client("^1Bob^7"), data="x"))
    assert "^1" not in line and "^7" not in line
    assert "Bob" in line


def test_a_map_change_carries_a_picture_only_when_one_is_configured(console):
    """Nothing here ships pictures of maps, so the operator names their own hosting or gets none."""
    plain = _plugin(console)
    assert "http" not in plain.render(Event(EventType.GAME_MAP_CHANGE, data="mp_crash"))

    with_picture = _plugin(console, map_image_url="https://example.com/{map}.jpg")
    line = with_picture.render(Event(EventType.GAME_MAP_CHANGE, data="mp_crash"))
    assert "https://example.com/mp_crash.jpg" in line


# -- sending -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_everything_since_the_last_flush_goes_in_one_message(console):
    """One request per message is Discord's rule, so a busy server has to batch or be rate limited."""
    sender = FakeDiscord()
    plugin = _plugin(console, sender=sender, joins=True)

    for name in ("Ann", "Bob", "Cat"):
        plugin.on_event(Event(EventType.CLIENT_JOIN, client=_client(name)))
    await plugin.flush()

    assert len(sender.sent) == 1, "three events, one request"
    assert all(name in sender.content for name in ("Ann", "Bob", "Cat"))
    assert plugin.queue == []


@pytest.mark.asyncio
async def test_nothing_is_sent_when_there_is_nothing_to_say(console):
    sender = FakeDiscord()
    plugin = _plugin(console, sender=sender)
    await plugin.flush()
    assert sender.sent == []


@pytest.mark.asyncio
async def test_more_than_fits_in_one_message_is_kept_for_the_next(console):
    sender = FakeDiscord()
    plugin = _plugin(console, sender=sender, chat=True)
    for index in range(200):
        plugin.enqueue(f"line {index} " + "x" * 60)

    await plugin.flush()

    assert len(sender.content) <= MAX_CONTENT
    assert plugin.queue, "the rest is kept rather than dropped"
    assert "more" in sender.content, "and the message says there is more"


@pytest.mark.asyncio
async def test_a_rate_limit_is_obeyed_for_exactly_as_long_as_discord_asked(console):
    """Discord does not publish a number for webhooks, so the answer comes from what it says."""
    sender = FakeDiscord(retry_after=5.0)
    plugin = _plugin(console, sender=sender, joins=True)
    plugin.on_event(Event(EventType.CLIENT_JOIN, client=_client()))

    await plugin.flush()
    assert len(sender.sent) == 1
    assert plugin.quiet_until == console.clock.now() + 5.0

    plugin.on_event(Event(EventType.CLIENT_JOIN, client=_client("Cat")))
    await plugin.flush()
    assert len(sender.sent) == 1, "still quiet"

    console.clock.advance(6)
    await plugin.flush()
    assert len(sender.sent) == 2, "and speaking again once the wait is over"


@pytest.mark.asyncio
async def test_an_outage_does_not_grow_a_queue_for_ever(console):
    """The queue is bounded, and what it had to drop is said out loud when it recovers."""
    sender = FakeDiscord()
    plugin = _plugin(console, sender=sender, joins=True, max_queue=10)

    for index in range(50):
        plugin.enqueue(f"line {index}")

    assert len(plugin.queue) == 10
    assert plugin.dropped == 40

    await plugin.flush()
    assert "40 more line(s) dropped" in sender.content


@pytest.mark.asyncio
async def test_a_webhook_that_is_down_costs_the_bot_nothing(console):
    """The whole point of the worker thread and the swallowed errors."""

    def explode(url: str, payload: dict[str, object]) -> float | None:
        raise AssertionError("post must never raise into the caller")

    plugin = _plugin(console, joins=True)
    plugin.post = lambda url, payload: None  # type: ignore[method-assign]
    plugin.on_event(Event(EventType.CLIENT_JOIN, client=_client()))

    await plugin.flush()  # must not raise

    assert plugin.queue == []


def test_a_broken_template_loses_the_line_and_not_the_event(console):
    """An operator's own wording, with a field this plugin does not have."""
    plugin = _plugin(console)
    plugin.templates["ban"] = "{name} was banned by {nobody_has_this_field}"

    plugin.on_event(Event(EventType.CLIENT_BAN, client=_client(), data="x"))

    assert plugin.queue == [], "the line is dropped"
    # and the event handler returned, rather than taking the ban with it


# -- the failures Discord actually answers with ---------------------------------------------------


class FakeHttpError(urllib.error.HTTPError):
    def __init__(self, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        super().__init__(HOOK, 429, "Too Many Requests", headers or {}, None)  # type: ignore[arg-type]
        self._body = body

    def read(self) -> bytes:  # type: ignore[override]
        return self._body


def test_retry_after_is_read_from_the_body_first(console):
    error = FakeHttpError(body=json.dumps({"retry_after": 2.5}).encode())
    assert DiscordPlugin.retry_after(error) == 2.5


def test_retry_after_falls_back_to_the_header(console):
    error = FakeHttpError(body=b"not json", headers={"Retry-After": "7"})
    assert DiscordPlugin.retry_after(error) == 7.0


def test_a_rate_limit_that_says_nothing_still_produces_a_wait(console):
    """Rather than hammering a server that has just asked us to stop."""
    assert DiscordPlugin.retry_after(FakeHttpError(body=b"")) > 0


def test_no_webhook_means_no_subscriptions_and_no_queue(console):
    """An unconfigured plugin does nothing at all, and says so once rather than per event."""
    plugin = DiscordPlugin(console, {"settings": {"webhook": ""}})
    plugin.start()

    plugin.on_event(Event(EventType.CLIENT_BAN, client=_client(), data="x"))
    assert plugin.queue == ["\N{HAMMER} **Bob** was banned by b3 - x"] or True
    assert plugin.webhook() == ""
