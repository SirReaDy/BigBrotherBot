"""The `discord` plugin — relaying the server to a channel, outbound only.

No test here reaches Discord. `post` is the one method that touches a socket, and every test replaces
it, which is what lets the interesting cases — a 429, a webhook that is down, a batch too long for
one message — be tested at all.

Six properties matter more than the formatting, and each has a test:

* **a Discord outage costs nothing** — the bot does not stall, the ban still happens, and the lines
  are not silently lost;
* **the queue is bounded**, because an outage otherwise grows one for as long as it lasts;
* **a rate limit is obeyed for as long as Discord asked**, not for a guess;
* **a player cannot reformat the channel with their own name**, which is the one part of these
  messages somebody else chooses;
* **the two styles say the same thing** — an operator who rewords a template gets their wording in
  the embed as well, or the setting is a trap; and
* **a setting that needs another plugin says so before the bot starts**, because the failure it
  otherwise produces is a channel that is quiet for a reason nobody can see.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.discord import MAX_CONTENT, MAX_EMBEDS, DiscordPlugin, Relayed

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
    def payload(self) -> dict[str, object]:
        return self.sent[-1] if self.sent else {}

    @property
    def embeds(self) -> list[dict]:
        return list(self.payload.get("embeds") or [])  # type: ignore[arg-type]

    @property
    def content(self) -> str:
        """Everything the last message said, whichever shape it was sent in.

        Tests about *what reached the channel* should not have to know which style is configured;
        the ones that are about the shape itself read `embeds` or the payload directly.
        """
        if "content" in self.payload:
            return str(self.payload["content"])
        return "\n".join(
            str(embed.get("description", "")) + json.dumps(embed.get("fields", []))
            for embed in self.embeds
        )


def _plugin(console, sender=None, **settings):  # noqa: ANN001, ANN202
    settings.setdefault("webhook", HOOK)
    plugin = DiscordPlugin(console, {"settings": settings})
    plugin.start()
    plugin.post = sender or FakeDiscord()  # type: ignore[method-assign]
    return plugin


def _client(name="Bob", cid="2", id_=7):  # noqa: ANN001, ANN202
    return Client(guid="B" * 32, name=name, cid=cid, id=id_)


def _texts(plugin: DiscordPlugin) -> list[str]:
    return [item.text for item in plugin.queue]


def _line(text: str) -> Relayed:
    """A queue entry for the tests that only care about volume, not about wording."""
    return Relayed(kind="chat", text=text)


# -- what gets relayed ---------------------------------------------------------------------------


def test_a_ban_is_relayed_with_who_and_why(console):
    plugin = _plugin(console)
    admin = _client("Admin", cid="1", id_=1)

    relayed = plugin.render(
        Event(EventType.CLIENT_BAN, client=_client(), target=admin, data="aimbot")
    )

    assert "Bob" in relayed.text and "Admin" in relayed.text and "aimbot" in relayed.text


def test_a_ban_with_no_reason_says_so_rather_than_trailing_off(console):
    plugin = _plugin(console)
    relayed = plugin.render(Event(EventType.CLIENT_BAN, client=_client()))
    assert "no reason given" in relayed.text


def test_an_automatic_ban_names_the_bot_rather_than_nobody(console):
    """Most bans here are not typed by anybody: a ban list match, a warning that ran out."""
    plugin = _plugin(console)
    relayed = plugin.render(Event(EventType.CLIENT_BAN, client=_client(), data="spam"))
    assert "b3" in relayed.text


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

    assert _texts(plugin) == ["Bob: hello everyone"]


def test_a_name_cannot_reformat_the_channel(console):
    """The name is the one part of these lines somebody else chooses, and Discord reads markdown."""
    plugin = _plugin(console)
    text = plugin.render(
        Event(EventType.CLIENT_BAN, client=_client("**@everyone**"), data="x")
    ).text

    assert "@everyone" not in text, "a mention in a player's name must not become a mention"
    assert text.count("**") == 2, "and their asterisks are not the ones that embolden the name"


def test_colour_codes_are_not_relayed_as_text(console):
    """`^1Bob^7` is a red Bob in game and four stray characters anywhere else."""
    plugin = _plugin(console)
    text = plugin.render(Event(EventType.CLIENT_BAN, client=_client("^1Bob^7"), data="x")).text
    assert "^1" not in text and "^7" not in text
    assert "Bob" in text


def test_a_map_change_carries_a_picture_only_when_one_is_configured(console):
    """Nothing here ships pictures of maps, so the operator names their own hosting or gets none."""
    plain = _plugin(console)
    assert plain.render(Event(EventType.GAME_MAP_CHANGE, data="mp_crash")).image == ""

    with_picture = _plugin(console, map_image_url="https://example.com/{map}.jpg")
    relayed = with_picture.render(Event(EventType.GAME_MAP_CHANGE, data="mp_crash"))
    assert relayed.image == "https://example.com/mp_crash.jpg"


def test_a_map_picture_goes_in_the_text_when_there_is_no_embed_to_hang_it_on(console):
    """`lines` has nowhere to put an image, and Discord renders a bare URL by itself."""
    plugin = _plugin(console, style="lines", map_image_url="https://example.com/{map}.jpg")
    relayed = plugin.render(Event(EventType.GAME_MAP_CHANGE, data="mp_crash"))
    assert "https://example.com/mp_crash.jpg" in relayed.text


# -- reports -------------------------------------------------------------------------------------


def test_a_report_names_the_reporter_as_prominently_as_the_reported(console):
    """An admin reading it an hour later has to decide about both of them."""
    plugin = _plugin(console, reports=True)
    reporter = _client("Ann", cid="1", id_=1)

    relayed = plugin.render(
        Event(EventType.CLIENT_REPORT, client=_client("Bob"), target=reporter, data="aimbot")
    )

    assert "Bob" in relayed.text and "Ann" in relayed.text and "aimbot" in relayed.text
    assert relayed.who == "Bob", "the card's first field is who was reported"
    assert relayed.by == "Ann", "and the footer says who reported them"


def test_reports_are_off_until_asked_for(console):
    plugin = _plugin(console)
    plugin.on_event(Event(EventType.CLIENT_REPORT, client=_client(), data="x"))
    assert plugin.queue == []


# -- the shape of a message ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_moderation_event_is_a_card_of_labelled_facts(console):
    """A card of labelled facts is scanned; a channel of sentences has to be read.

    An author line naming the server, the facts as fields somebody can read a column at a time, and
    who did it along the bottom with the time.
    """
    sender = FakeDiscord()
    console.game.hostname = "^1Local ^7COD4X Server"
    console.game.map_name = "mp_crash"
    plugin = _plugin(console, sender=sender)
    admin = _client("Admin", cid="1", id_=1)

    plugin.on_event(Event(EventType.CLIENT_BAN, client=_client(), target=admin, data="aimbot"))
    await plugin.flush()

    embed = sender.embeds[0]
    assert embed["author"]["name"] == "Local COD4X Server", "colour codes gone, as everywhere"
    assert embed["title"] == "Ban"
    named = [(f["name"], f["value"], f["inline"]) for f in embed["fields"]]
    assert named[:3] == [
        ("Banned Player", "Bob", True),
        ("By", "Admin", True),
        ("Map", "mp_crash", True),
    ], "three columns, which is a full row"
    assert ("Reason", "aimbot", False) in named
    assert "footer" not in embed, "the server is named once, at the top"
    assert embed["timestamp"], "so Discord prints when it happened"


@pytest.mark.asyncio
async def test_an_event_with_nothing_to_label_keeps_the_operators_wording(console):
    """There is nothing to put in a field about "Bob: hello everyone", so it stays a sentence.

    Which is also what keeps `templates` meaningful in this style: the events with no fields are
    exactly the ones whose whole content is the operator's own wording.
    """
    sender = FakeDiscord()
    plugin = _plugin(console, sender=sender, chat=True)
    plugin.templates["chat"] = "<{name}> {text}"

    plugin.on_event(Event(EventType.CLIENT_SAY, client=_client(), data="hello everyone"))
    await plugin.flush()

    assert sender.embeds[0]["description"] == "<Bob> hello everyone"
    assert "fields" not in sender.embeds[0]


@pytest.mark.asyncio
async def test_the_card_carries_the_current_maps_picture_when_one_is_hosted(console):
    """The reference plugin's map shot in the corner — from the operator's own hosting, not a wiki."""
    sender = FakeDiscord()
    console.game.map_name = "mp_crash"
    plugin = _plugin(console, sender=sender, map_image_url="https://example.com/{map}.jpg")

    plugin.on_event(Event(EventType.CLIENT_KICK, client=_client(), data="afk"))
    await plugin.flush()

    assert sender.embeds[0]["thumbnail"] == {"url": "https://example.com/mp_crash.jpg"}


@pytest.mark.asyncio
async def test_a_map_change_shows_the_picture_full_width(console):
    """The one event that is *about* the picture does not get it shrunk into the corner."""
    sender = FakeDiscord()
    plugin = _plugin(console, sender=sender, map_image_url="https://example.com/{map}.jpg")

    plugin.on_event(Event(EventType.GAME_MAP_CHANGE, data="mp_crash"))
    await plugin.flush()

    assert sender.embeds[0]["image"] == {"url": "https://example.com/mp_crash.jpg"}
    assert "thumbnail" not in sender.embeds[0]


@pytest.mark.asyncio
async def test_each_kind_of_event_gets_its_own_colour(console):
    """Nobody reads a moderation channel word by word; they look for the red ones."""
    sender = FakeDiscord()
    plugin = _plugin(console, sender=sender, joins=True)

    plugin.on_event(Event(EventType.CLIENT_BAN, client=_client(), data="x"))
    plugin.on_event(Event(EventType.CLIENT_JOIN, client=_client("Ann")))
    await plugin.flush()

    ban, join = sender.embeds
    assert ban["color"] != join["color"]
    assert ban["fields"][0]["name"] == "Banned Player"
    assert join["title"] == "Joined", "an arrival has no facts to label, so it keeps a heading"


@pytest.mark.asyncio
async def test_lines_style_posts_plain_text_and_no_embeds(console):
    sender = FakeDiscord()
    plugin = _plugin(console, sender=sender, style="lines", joins=True)

    plugin.on_event(Event(EventType.CLIENT_JOIN, client=_client("Ann")))
    await plugin.flush()

    assert "embeds" not in sender.payload
    assert "Ann joined" in str(sender.payload["content"])


@pytest.mark.asyncio
async def test_no_more_than_ten_embeds_go_in_one_message(console):
    """Discord's limit, and the rest waits rather than being dropped."""
    sender = FakeDiscord()
    plugin = _plugin(console, sender=sender, joins=True)
    for index in range(25):
        plugin.enqueue(_line(f"line {index}"))

    await plugin.flush()

    assert len(sender.embeds) == MAX_EMBEDS
    assert len(plugin.queue) == 15, "the rest is kept for the next flush"


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
    plugin = _plugin(console, sender=sender, style="lines", chat=True)
    for index in range(200):
        plugin.enqueue(_line(f"line {index} " + "x" * 60))

    await plugin.flush()

    assert len(str(sender.payload["content"])) <= MAX_CONTENT
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
        plugin.enqueue(_line(f"line {index}"))

    assert len(plugin.queue) == 10
    assert plugin.dropped == 40

    await plugin.flush()
    assert "40 more line(s) dropped" in sender.content


@pytest.mark.asyncio
async def test_a_webhook_that_is_down_costs_the_bot_nothing(console):
    """The whole point of the worker thread and the swallowed errors."""
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


# -- settings that only the whole config can judge -------------------------------------------------


def test_relaying_reports_without_the_report_plugin_is_refused(console):
    """The failure it otherwise makes is a channel that is quiet, which nobody can see from in game."""
    problems = DiscordPlugin.check_config(
        {"settings": {"reports": True}}, frozenset({"admin", "discord"})
    )
    assert len(problems) == 1
    assert "report" in problems[0] and "reports: no" in problems[0]


def test_relaying_reports_with_the_report_plugin_is_fine(console):
    assert (
        DiscordPlugin.check_config(
            {"settings": {"reports": True}}, frozenset({"admin", "discord", "report"})
        )
        == []
    )


def test_an_unknown_style_is_named_rather_than_guessed_at(console):
    problems = DiscordPlugin.check_config({"settings": {"style": "fancy"}}, frozenset({"discord"}))
    assert len(problems) == 1 and "fancy" in problems[0]


def test_the_default_config_asks_for_nothing(console):
    assert DiscordPlugin.check_config({}, frozenset({"admin", "discord"})) == []


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

    assert plugin.webhook() == ""


@pytest.mark.asyncio
async def test_a_tempban_says_how_long_in_the_words_the_game_used(console):
    """ "a while" was all this could say: the event carried no figure to print."""
    sender = FakeDiscord()
    plugin = _plugin(console, sender=sender)
    admin = _client("SirReaDy", cid="1", id_=1)

    plugin.on_event(
        Event(
            EventType.CLIENT_BAN_TEMP,
            client=_client("bota"),
            target=admin,
            data="cheating",
            extra={"duration": 20160},
        )
    )
    await plugin.flush()

    embed = sender.embeds[0]
    assert {"name": "For", "value": "14 days", "inline": True} in embed["fields"]
    assert {"name": "By", "value": "SirReaDy", "inline": True} in embed["fields"]


@pytest.mark.asyncio
async def test_a_map_with_no_picture_of_its_own_gets_the_placeholder(console):
    """Ten shots and a rotation of thirty should not mean two shapes of card."""
    sender = FakeDiscord()
    plugin = _plugin(
        console, sender=sender, map_image_fallback="https://example.com/unknown-map.png"
    )

    plugin.on_event(Event(EventType.GAME_MAP_CHANGE, data="mp_shipment"))
    await plugin.flush()

    assert sender.embeds[0]["image"] == {"url": "https://example.com/unknown-map.png"}


def test_the_placeholder_never_overrides_a_picture_that_exists(console):
    plugin = _plugin(
        console,
        map_image_url="https://example.com/{map}.jpg",
        map_image_fallback="https://example.com/unknown-map.png",
    )
    relayed = plugin.render(Event(EventType.GAME_MAP_CHANGE, data="mp_crash"))
    assert relayed.image == "https://example.com/mp_crash.jpg"


@pytest.mark.asyncio
async def test_a_card_is_widened_to_the_message_column(console):
    """Discord offers no width of its own, so the lever is an invisible field.

    A card of three short values is a narrow card. Figure spaces (U+2007) are invisible and Discord
    does not collapse them the way it collapses ordinary spaces, so a last field of them pushes the
    embed out to the width of the message column and stops there. The honest one of the two tricks
    available: the other is a wide transparent image, which means hosting a file and having every
    card fetch it.
    """
    sender = FakeDiscord()
    plugin = _plugin(console, sender=sender)

    plugin.on_event(Event(EventType.CLIENT_KICK, client=_client(), data="afk"))
    await plugin.flush()

    spacer = sender.embeds[0]["fields"][-1]
    assert spacer["name"] == "​", "an empty name, which is all Discord accepts"
    assert set(spacer["value"]) == {" "} and len(spacer["value"]) == 60
    assert spacer["inline"] is False, "a row of its own, or it widens nothing"


@pytest.mark.asyncio
async def test_the_widening_can_be_switched_off(console):
    sender = FakeDiscord()
    plugin = _plugin(console, sender=sender, full_width=False)

    plugin.on_event(Event(EventType.CLIENT_KICK, client=_client(), data="afk"))
    await plugin.flush()

    names = [f["name"] for f in sender.embeds[0]["fields"]]
    assert "​" not in names


@pytest.mark.asyncio
async def test_a_map_change_does_not_carry_a_map_field(console):
    """The map is the whole message there; a column repeating it is chrome."""
    sender = FakeDiscord()
    console.game.map_name = "mp_crash"
    plugin = _plugin(console, sender=sender)

    plugin.on_event(Event(EventType.GAME_MAP_CHANGE, data="mp_shipment"))
    await plugin.flush()

    assert "mp_shipment" in sender.embeds[0]["description"]
    assert "fields" not in sender.embeds[0], "nothing to label, so nothing is labelled"
