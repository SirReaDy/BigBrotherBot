"""BeParser — the messages a BattlEye server pushes down its RCON socket.

There is no log file here, so "a line" is one server message (see :mod:`b3.net.battleye`). The
grammar is small and entirely unlike the two file-based families:

    Player #0 Bravo17 (76.108.91.78:2304) connected
    Verified GUID (80a5885ebe2420bab5e1581234567890) of player #0 Bravo17
    Player #0 Bravo17 - GUID: 80a5885ebe2420bab5e1581234567890 (unverified)
    (Global) Bravo17: hello b3
    Player #4 Kauldron disconnected
    Player #2 NZ (04b81a0bd914e7ba610ef31234567890) has been kicked by BattlEye: Script Restriction #107
    RCon admin #0: (Global) server going down
    Script Log: #6 Playername (bbb6476155852ac2ab30121234567890) - #2 "bp_id"…

Two things about it shape everything below.

**Identity arrives separately from the connection.** The connect message has a name, a slot and an IP
but *no* GUID; the GUID follows in its own message once BattlEye has checked it, first as
``unverified`` and then as ``Verified``. So the join event the rest of the bot acts on is published
when the GUID lands, not when the player connects — otherwise every player would be authenticated
against an empty identity.

**Chat names the speaker, not their slot** — the same problem the Quake3 family has, solved the same
way: the parser remembers who is in which slot, and chat from a name it has never seen produces no
event rather than a nameless client.
"""

from __future__ import annotations

import logging
import re

from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.parsers.base import Parser
from b3.parsers.registry import handles

log = logging.getLogger(__name__)

#: Chat channels a player can talk on. Arma's restricted channels are mapped to team chat, which the
#: classic parser did not do — it reported every channel as a plain say, so a censor or spam plugin
#: could not tell a squad message from a server-wide one.
TEAM_CHANNELS = frozenset({"Group", "Side", "Vehicle", "Command"})
PUBLIC_CHANNELS = frozenset({"Global", "Direct", "Lobby", "Unknown"})


class BeParser(Parser):
    """Arma 2 / Arma 3. Selected by ``family="battleye"`` in the profile."""

    #: BattlEye messages carry no timestamp, so nothing should be stripped from the front of one.
    #: The shared pattern would eat the start of a chat line that happened to open with a clock time.
    _timestamp_re = re.compile(r"(?!)")

    # -- lifecycle ----------------------------------------------------------

    @handles(
        r"^Player #(?P<cid>\d+) (?P<name>.+) \((?P<ip>[0-9.]+):(?P<port>\d+)\) connected\s*$"
    )
    def on_connected(self, m: "re.Match[str]") -> Event:
        """``Player #0 Bravo17 (76.108.91.78:2304) connected`` — name and IP, but no GUID yet."""
        client = self._get_or_create(m["cid"], m["name"])
        client.ip = m["ip"]
        client.authed = False
        return Event(EventType.CLIENT_CONNECT, client=client)

    @handles(
        r"^Verified GUID \((?P<guid>[0-9a-f]+)\) of player #(?P<cid>\d+) (?P<name>.+?)\s*$",
        re.IGNORECASE,
    )
    def on_verified_guid(self, m: "re.Match[str]") -> Event:
        """``Verified GUID (80a5…) of player #0 Bravo17`` — the join the rest of the bot acts on.

        BattlEye has checked this id with its own backend, which is what makes it worth trusting as
        the identity a ban is attached to.
        """
        return self._identify(m["cid"], m["name"], m["guid"], verified=True)

    @handles(
        r"^Player #(?P<cid>\d+) (?P<name>.+) - GUID: (?P<guid>[0-9a-f]+) \(unverified\)\s*$",
        re.IGNORECASE,
    )
    def on_unverified_guid(self, m: "re.Match[str]") -> Event | None:
        """The same id before BattlEye has checked it.

        Recorded, but it does **not** authenticate anybody unless the profile says to
        (``trust_unverified_guid``): an unverified GUID is what the client claimed, and taking it at
        face value is how somebody else's ban gets applied to the wrong player — or dodged.
        """
        return self._identify(
            m["cid"], m["name"], m["guid"], verified=self.profile.trust_unverified_guid
        )

    def _identify(self, cid: str, name: str, guid: str, *, verified: bool) -> Event:
        client = self._get_or_create(cid, name)
        if not client.guid:
            client.guid = guid
        if not verified:
            return Event(EventType.CLIENT_UPDATE, client=client)
        client.authed = False  # so the runtime authenticates them against the database
        return Event(EventType.CLIENT_JOIN, client=client)

    @handles(r"^Player #(?P<cid>\d+) (?P<name>.+) disconnected\s*$")
    def on_disconnected(self, m: "re.Match[str]") -> Event | None:
        client = self.clients.remove(m["cid"])
        if client is None:
            return None
        return Event(EventType.CLIENT_DISCONNECT, client=client)

    @handles(
        r"^Player #(?P<cid>\d+) (?P<name>.+) \((?P<guid>[0-9a-f]+)\) has been kicked by BattlEye: "
        r"(?P<reason>.*)$",
        re.IGNORECASE,
    )
    def on_battleye_kick(self, m: "re.Match[str]") -> Event | None:
        """BattlEye's *own* kick — a script restriction, a bad GUID, its global ban list.

        Published as a disconnect carrying the reason, because that is what happened: the player is
        gone and the bot did not do it. It is deliberately not recorded as one of our penalties —
        our penalty table is what *this bot* decided, and mixing the anti-cheat's verdicts into it
        would corrupt a player's ban history.
        """
        client = self.clients.remove(m["cid"])
        if client is None:
            return None
        return Event(EventType.CLIENT_DISCONNECT, client=client, data=f"BattlEye: {m['reason']}")

    # -- chat ---------------------------------------------------------------

    @handles(r"^\((?P<channel>[A-Za-z]+)\) (?P<rest>.+)$")
    def on_say(self, m: "re.Match[str]") -> Event | None:
        """``(Global) Bravo17: hello b3``. The channel decides public versus team chat.

        The speaker is found by matching the *known* names against the front of the line rather than
        by splitting on the first colon. An Arma player can have a colon in their name, and splitting
        would leave them permanently unidentifiable — which is to say immune to every plugin that
        reads chat, censor and spam control included.
        """
        channel = m["channel"]
        if channel not in PUBLIC_CHANNELS and channel not in TEAM_CHANNELS:
            return None  # not a chat channel we know; let it fall through as unhandled
        found = self._split_speaker(m["rest"])
        if found is None:
            return None
        client, text = found
        etype = EventType.CLIENT_TEAM_SAY if channel in TEAM_CHANNELS else EventType.CLIENT_SAY
        return Event(etype, data=text, client=client, extra={"channel": channel})

    @handles(r"^RCon admin #(?P<admin>\d+): (?P<text>.*)$")
    def on_rcon_admin(self, _m: "re.Match[str]") -> None:
        """Another RCON tool talking, or the echo of our own `say`. Never a player, so ignored.

        Handled explicitly rather than left to fall through: our own chat comes back on this
        channel, and matching it against the chat pattern instead would feed the bot its own
        output — including its own command replies.
        """
        return None

    @handles(
        r"^(?P<kind>\w[\w ]*) Log: #(?P<cid>\d+) (?P<name>.+?) \((?P<guid>[0-9a-f]+)\)"
        r"(?P<rest>.*)$",
        re.IGNORECASE,
    )
    def on_script_log(self, m: "re.Match[str]") -> Event:
        """``Script Log: #6 Player (bbb6…) - …`` — BattlEye's anti-cheat logging.

        This is what an Arma anti-cheat plugin reads, so it is published rather than dropped: as
        `CLIENT_ACTION` with the log kind, which keeps it inside the shared vocabulary instead of
        inventing an event only one game can raise.
        """
        client = self._get_or_create(m["cid"], m["name"])
        if not client.guid:
            client.guid = m["guid"]
        return Event(
            EventType.CLIENT_ACTION,
            data=f"battleye_{m['kind'].strip().lower().replace(' ', '_')}_log",
            client=client,
            extra={"detail": m["rest"].strip()},
        )

    # -- helpers ------------------------------------------------------------

    def _get_or_create(self, cid: str, name: str | None = None) -> Client:
        client = self.clients.get_by_cid(cid)
        if client is None:
            client = Client(cid=cid, name=(name or "").strip())
            self.clients.add(client)
        elif name:
            client.name = name.strip()
        return client

    def _split_speaker(self, rest: str) -> tuple[Client, str] | None:
        """Split ``<name>: <text>`` using the names the bot already knows.

        Longest name first, so a player called "Bob" cannot claim a line spoken by "Bob: the
        builder". A name the bot has never seen yields nothing rather than a nameless client — the
        same rule the Quake3 family follows, and for the same reason: chat identifies the speaker by
        name, and inventing a client from one would make every unknown name a new player.
        """
        for client in sorted(self.clients.connected(), key=lambda c: len(c.name), reverse=True):
            prefix = f"{client.name}: "
            if client.name and rest.startswith(prefix):
                return client, rest[len(prefix) :]
        return None
