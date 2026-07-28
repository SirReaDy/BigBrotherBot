"""AltParser — Altitude's log, which is one JSON object per line.

    {"port":27276,"time":103344,"map":"ball_cave","type":"mapLoading"}
    {"port":27276,"time":12108767,"player":2,"nickname":"Courgette","vaporId":"a865…","type":"clientAdd"}
    {"port":27276,"time":3571497,"player":-1,"victim":0,"source":"plane","xp":10,"type":"kill"}

So this is the second family whose grammar is not regex-shaped, and like
:mod:`b3.parsers.frostbite.parser` it dispatches on a field — ``type`` — through a dict rather than
using the ``@handles`` router. The reasoning is the same: the structure is already parsed, and
flattening it to text to match it with a regex would throw that away.

Three things are specific to this engine and worth knowing before changing anything here.

**One log file can hold several servers.** Every line carries the ``port`` that produced it, and an
Altitude installation running three servers writes all three into one file. Lines from another port
are not ours, and acting on them would ban a player on the wrong server.

**Identity is the ``vaporId``**, a 36-character UUID that arrives with ``clientAdd`` — so unlike the
CoD engines there is no second-phase lookup to do, and unlike Frostbite there is no separate
authentication event. The all-zero UUID is not an identity: it is what the server reports for bots
and for itself, so it must never be authenticated or a single database row would accumulate every
bot that ever played.

**The classic parser fired the join on *spawn*.** That is corrected here: this bot authenticates a
player when ``CLIENT_JOIN`` fires, so joining on the spawn line meant a banned player who connected
and sat in the spectator seats was never checked at all, and never would be until they flew. The join
belongs where the identity arrives, which is ``clientAdd``; the spawn is a ``CLIENT_SPAWN``, an event
this vocabulary gained with Frostbite.

Two pieces of the classic parser are deliberately **not** here, because they are policy rather than
parsing and this bot keeps parsers pure (a line in, events out — no database reads, no RCON):

* lifting the game's own two-minute vote-kick ban for anyone above level 2. The reason reaches
  plugins on the disconnect event, and the policy belongs with the `callvote` plugin.
* tempbanning a player who calls a kick vote against a higher-level admin. Same reason — and the
  classic implementation of it never ran anyway: it read ``vaporId`` from an event whose field is
  called ``source``, so every vote raised a ``KeyError`` instead.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from b3.core.clients import ClientManager
from b3.core.events import Event, EventType
from b3.core.game import PlayerInfo
from b3.domain.client import Client
from b3.parsers.base import Parser
from b3.parsers.profile import GameProfile

log = logging.getLogger(__name__)

#: The vapor id the server reports for a bot, and for itself. Never a player account.
NOBODY = "00000000-0000-0000-0000-000000000000"

#: The two ``clientRemove`` reasons the classic parser knew. Any other reason still means the player
#: is gone — matching on the string to decide *that* is how an unrecognised reason leaves a ghost in
#: the roster forever.
REASON_LEFT = "Client left."
REASON_VOTE_KICK = "Kicked by vote."


@dataclass(frozen=True, slots=True)
class KillData:
    """Payload for an Altitude kill.

    Neither of the other two shapes: this engine reports no damage figure and no hit location (so the
    classic parser's hard-coded ``100`` damage was invented), but it does report the experience the
    kill was worth and the killer's streak.
    """

    weapon: str
    xp: int = 0
    streak: int = 0
    multi: int = 0


class AltParser(Parser):
    """Altitude. Selected by ``family="altitude"`` in the profile."""

    def __init__(self, profile: GameProfile, clients: ClientManager | None = None) -> None:
        super().__init__(profile, clients)
        #: Last ``pingSummary``, slot -> ping. None until one arrives, which is not the same as an
        #: empty one: see :meth:`get_players`.
        self._pings: dict[str, int] | None = None
        #: `type` -> handler. See the module docstring for why this is a dict and not `@handles`.
        #: A handler may return several events: a slot reused without a departure line reports the
        #: departure *and* the arrival.
        self._handlers: dict[str, Callable[[dict[str, Any]], Event | list[Event] | None]] = {
            "serverInit": self._on_server_init,
            "serverStart": self._on_server_start,
            "mapLoading": self._on_map_loading,
            "mapChange": self._on_map_change,
            "clientAdd": self._on_client_add,
            "clientRemove": self._on_client_remove,
            "chat": self._on_chat,
            "kill": self._on_kill,
            "assist": self._on_assist,
            "teamChange": self._on_team_change,
            "spawn": self._on_spawn,
            "roundEnd": self._on_round_end,
            "powerupPickup": self._on_powerup_pickup,
            "powerupAutoUse": self._on_powerup_pickup,
            "powerupUse": self._on_powerup_use,
            "powerupDefuse": self._on_powerup_defuse,
            "goal": self._on_goal,
            "structureDamage": self._on_structure_damage,
            "structureDestroy": self._on_structure_destroy,
            "pingSummary": self._on_ping_summary,
            "consoleCommandExecute": self._on_console_command,
        }
        #: Console commands that map to an event of their own; anything else is reported as-is.
        self._commands: dict[str, Callable[[dict[str, Any], Client], Event | None]] = {
            "vote": self._on_call_vote,
            "castBallot": self._on_cast_ballot,
        }

    # -- dispatch ----------------------------------------------------------

    def parse_line(self, line: str) -> list[Event]:
        """Read one JSON event and route it on its ``type``."""
        event = self._decode(line)
        if event is None:
            return []
        if not self._is_ours(event):
            return []
        etype = event.get("type")
        if not isinstance(etype, str):
            log.warning("altitude: log line with no usable type: %r", line[:80])
            return []
        handler = self._handlers.get(etype)
        if handler is None:
            # An Altitude server emits plenty this bot has no use for, and a line per unknown type
            # would bury the log. Debug, so `-v` still shows what is being skipped.
            log.debug("altitude: no handler for event type %r", etype)
            return []
        result = handler(event)
        if result is None:
            return []
        if isinstance(result, Event):
            return [result]
        return list(result)

    @staticmethod
    def _decode(line: str) -> dict[str, Any] | None:
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Said once per line, and worth saying: on this engine a line that is not JSON means the
            # bot is pointed at the wrong file (the server's stdout, say, instead of its log).
            log.warning("altitude: expected one JSON object per line, got %r", line[:60])
            return None
        if not isinstance(event, dict):
            log.warning("altitude: expected a JSON object, got %s", type(event).__name__)
            return None
        return event

    def _is_ours(self, event: dict[str, Any]) -> bool:
        """Whether this line came from the server this bot is watching.

        With no port configured nothing is filtered: a single-server installation still works, and
        refusing every line would be the silent "the bot sees nothing" failure. With one configured,
        a line from a neighbouring server is dropped -- it is exactly as real as ours, and acting on
        it would kick a player on a server this bot has no business touching.
        """
        if not self.port:
            return True
        port = event.get("port")
        if port is None:
            return True  # not a per-server line; nothing to disagree with
        return int(port) == self.port

    # -- identity ----------------------------------------------------------

    def _client(self, slot: object) -> Client | None:
        """The player in ``slot``, or None for the world and for a slot nobody is in.

        Nothing is created here. A player this bot has not seen join cannot be given an identity —
        there is no roster query on this engine to ask — and inventing a guid-less client would put
        a row in the database that can never be matched to the same person again.
        """
        if slot is None:
            return None
        cid = str(slot)
        if cid == self.profile.world_cid:
            return None
        return self.clients.get_by_cid(cid)

    def _on_client_add(self, event: dict[str, Any]) -> list[Event] | None:
        """``clientAdd`` — a player connected, with their identity attached.

        The vapor id makes this the join: everything the bot needs to look them up in the database
        is on this one line. A bot player (the all-zero id) still becomes a client, because it
        occupies a slot and shows up in chat and kills — it simply gets no identity, so it is never
        authenticated and never stored.

        If somebody else is already recorded in that slot, their departure is reported first. That
        only happens when a ``clientRemove`` was missed, and on this engine nothing else would ever
        notice: every other family reconciles the roster against the server every five minutes, and
        here there is no roster to ask for. Without this the previous occupant would be dropped from
        the client store silently — no ``CLIENT_DISCONNECT``, so anything counting who is on the
        server keeps them forever.
        """
        slot = event.get("player")
        if slot is None:
            return None
        cid = str(slot)
        vapor_id = _identity(event.get("vaporId"))
        # An "ip" here is "address:port"; only the address is the player's.
        address = str(event.get("ip") or "").rsplit(":", 1)[0]

        events: list[Event] = []
        previous = self.clients.get_by_cid(cid)
        if previous is not None and not _same_player(previous, vapor_id):
            log.info(
                "altitude: slot %s is now %s; %s left without a clientRemove line",
                cid,
                event.get("nickname"),
                previous.name,
            )
            self.clients.remove(cid)
            events.append(
                Event(
                    EventType.CLIENT_DISCONNECT,
                    data="",
                    client=previous,
                    extra={"reason": "", "vote_kick": False, "inferred": True},
                )
            )

        client = Client(
            cid=cid,
            name=str(event.get("nickname") or ""),
            guid=vapor_id,
            ip=address,
        )
        self.clients.add(client)
        events.append(
            Event(
                EventType.CLIENT_JOIN,
                client=client,
                extra={"level": event.get("level"), "ace_rank": event.get("aceRank")},
            )
        )
        return events

    def _on_client_remove(self, event: dict[str, Any]) -> Event | None:
        """``clientRemove`` — a player left, was kicked, or was vote-kicked.

        The reason is passed through rather than matched on. The classic parser branched on the two
        strings it knew and logged anything else as unknown, which is fine — but *this* has to hold
        for a reason it has never seen too: the player is gone either way, and a roster that only
        forgets people for known reasons keeps ghosts forever.

        The slot is checked against the vapor id on the line before anyone is removed. The classic
        parser looked the player up *by* vapor id, which made it immune to a line arriving after the
        slot had been taken by somebody else; keying on the slot is right for the rest of this bot,
        so the identity is verified instead. Otherwise a late departure line would disconnect the
        player who is standing in that slot now — and on this engine nothing would put them back.
        """
        client = self._client(event.get("player"))
        if client is None:
            return None
        if not _same_player(client, _identity(event.get("vaporId"))):
            log.info(
                "altitude: ignoring a clientRemove for slot %s: %s is in it now",
                event.get("player"),
                client.name,
            )
            return None
        if client.cid is not None:
            self.clients.remove(client.cid)
        reason = str(event.get("reason") or "")
        return Event(
            EventType.CLIENT_DISCONNECT,
            data=reason,
            client=client,
            # `vote_kick` is what the (unported) policy of lifting the game's own two-minute ban for
            # an admin needs, and naming it here means the plugin that does it never has to know the
            # game's exact wording.
            extra={"reason": reason, "vote_kick": reason == REASON_VOTE_KICK},
        )

    # -- chat --------------------------------------------------------------

    def _on_chat(self, event: dict[str, Any]) -> Event | None:
        """``chat`` — and there is no team-chat variant; the classic parser noted the same.

        ``server: true`` is the bot's *own* output coming back: everything sent with
        ``serverMessage`` or ``serverWhisper`` is echoed into the log. Reading those as a player
        talking would make the bot answer itself, so they are reported as a custom event and nothing
        routes them to the command processor.
        """
        message = str(event.get("message") or "")
        if event.get("server"):
            return Event(EventType.CUSTOM, data=message, extra={"kind": "server_message"})
        client = self._client(event.get("player"))
        if client is None:
            return None
        return Event(EventType.CLIENT_SAY, data=message, client=client)

    # -- combat ------------------------------------------------------------

    def _on_kill(self, event: dict[str, Any]) -> Event | None:
        """``kill`` — ``player`` killed ``victim`` with ``source``.

        ``player: -1`` is the world: a plane flown into the ground, or the server. That is published
        as a suicide, the way Frostbite's killer-less kill is, rather than crediting a kill to a
        phantom "WORLD" client — which the classic parser created as a real client, hidden but in the
        client store.
        """
        victim = self._client(event.get("victim"))
        if victim is None:
            return None
        data = KillData(
            weapon=str(event.get("source") or ""),
            xp=_as_int(event.get("xp")),
            streak=_as_int(event.get("currentStreak") or event.get("streak")),
            multi=_as_int(event.get("multi")),
        )
        attacker = self._client(event.get("player"))
        if attacker is None or attacker is victim:
            return Event(EventType.CLIENT_SUICIDE, data=data, client=victim, target=victim)
        # No team-kill branch: friendly fire is off in this game, so the server does not report one.
        # If a title ever does, the shared `attacker.team == victim.team` test belongs here.
        return Event(EventType.CLIENT_KILL, data=data, client=attacker, target=victim)

    def _on_assist(self, event: dict[str, Any]) -> Event | None:
        """``assist`` — helped kill ``victim``."""
        attacker = self._client(event.get("player"))
        victim = self._client(event.get("victim"))
        if attacker is None or victim is None:
            return None
        return Event(EventType.CLIENT_ACTION, data="assist", client=attacker, target=victim)

    def _on_spawn(self, event: dict[str, Any]) -> Event | None:
        """``spawn`` — into a plane, with a team and three perks.

        The classic parser reported this as the join (see the module docstring) and created a client
        here if it had not seen one. Neither is done: a spawn is a spawn, and a player whose
        ``clientAdd`` was missed has no identity to invent.
        """
        client = self._client(event.get("player"))
        if client is None:
            return None
        self._set_team(client, event.get("team"))
        return Event(
            EventType.CLIENT_SPAWN,
            client=client,
            extra={
                "plane": event.get("plane"),
                "skin": event.get("skin"),
                "perks": [event.get("perkRed"), event.get("perkGreen"), event.get("perkBlue")],
            },
        )

    def _on_team_change(self, event: dict[str, Any]) -> Event | None:
        """``teamChange``."""
        client = self._client(event.get("player"))
        if client is None:
            return None
        self._set_team(client, event.get("team"))
        return Event(EventType.CLIENT_TEAM_CHANGE, data=client.team, client=client)

    def _set_team(self, client: Client, raw: object) -> None:
        if raw is None:
            return
        client.team = self.profile.teams.get(str(raw), "")

    # -- objectives --------------------------------------------------------

    def _on_powerup_pickup(self, event: dict[str, Any]) -> Event | None:
        """``powerupPickup`` and ``powerupAutoUse`` — both are "they have it now"."""
        client = self._client(event.get("player"))
        if client is None:
            return None
        return Event(
            EventType.CLIENT_ITEM_PICKUP,
            data=str(event.get("powerup") or ""),
            client=client,
            extra=_position(event),
        )

    def _on_powerup_use(self, event: dict[str, Any]) -> Event | None:
        """``powerupUse`` — fired or dropped, which is not in the shared vocabulary."""
        client = self._client(event.get("player"))
        if client is None:
            return None
        return Event(
            EventType.CUSTOM,
            data=str(event.get("powerup") or ""),
            client=client,
            extra={"kind": "powerup_use", **_position(event)},
        )

    def _on_powerup_defuse(self, event: dict[str, Any]) -> Event | None:
        client = self._client(event.get("player"))
        if client is None:
            return None
        return Event(EventType.CLIENT_ACTION, data="defuse", client=client, extra=_position(event))

    def _on_goal(self, event: dict[str, Any]) -> Event | None:
        client = self._client(event.get("player"))
        if client is None:
            return None
        assister = self._client(event.get("assister"))
        return Event(EventType.CLIENT_ACTION, data="goal", client=client, target=assister)

    def _on_structure_damage(self, event: dict[str, Any]) -> Event | None:
        return self._structure(event, "structure_damage")

    def _on_structure_destroy(self, event: dict[str, Any]) -> Event | None:
        return self._structure(event, "structure_destroy")

    def _structure(self, event: dict[str, Any], action: str) -> Event | None:
        """A base or turret was hit or destroyed.

        ``target`` here is the *kind of structure* — "base", "turret" — and the classic parser put
        that string into the event's ``target`` field, which every other event uses for a Client. Any
        plugin reading ``event.target.name`` would have raised on it. It goes in ``extra``.
        """
        client = self._client(event.get("player"))
        if client is None:
            return None
        return Event(
            EventType.CLIENT_ACTION,
            data=action,
            client=client,
            extra={"structure": str(event.get("target") or "")},
        )

    # -- the match ---------------------------------------------------------

    def _on_server_init(self, event: dict[str, Any]) -> Event | None:
        """``serverInit`` — the server's name and player limit, on startup.

        Published as ``SERVER_INFO``, which the runtime records in :class:`b3.core.game.Game`. Every
        other family here reports the same two values as cvars; this engine states them outright.
        """
        return Event(
            EventType.SERVER_INFO,
            data=str(event.get("name") or ""),
            extra={"max_players": _as_int(event.get("maxPlayerCount"))},
        )

    def _on_server_start(self, event: dict[str, Any]) -> Event | None:
        return Event(EventType.CUSTOM, extra={"kind": "server_start"})

    def _on_map_loading(self, event: dict[str, Any]) -> Event | None:
        """``mapLoading`` — announced before ``mapChange``, so it is a warmup, not a start."""
        return Event(
            EventType.GAME_WARMUP, data=str(event.get("map") or ""), extra={"kind": "map_loading"}
        )

    def _on_map_change(self, event: dict[str, Any]) -> Event | None:
        """``mapChange`` — the new map is live.

        Published as ``GAME_ROUND_START`` carrying a cvar-shaped payload, which is what the runtime
        reads to update :class:`b3.core.game.Game`. The classic parser published a *warmup* here, so
        nothing recorded the map at all: `!map` and every plugin asking what was being played got the
        previous answer forever.
        """
        return Event(
            EventType.GAME_ROUND_START,
            data={
                "mapname": str(event.get("map") or ""),
                "g_gametype": str(event.get("mode") or ""),
            },
        )

    def _on_round_end(self, event: dict[str, Any]) -> Event | None:
        """``roundEnd`` — a stats block, one column per participant.

        The block is transposed into per-player stats here, because it arrives as
        ``{"Kills": [22, 12, …]}`` alongside ``"participants": [0, 1, …]`` and matching the two up by
        index is the sort of thing that should happen once, not in every plugin that wants a score.
        The classic parser also stored ``winnerByAward`` — but stored ``participantStatsByName``
        under that key by mistake, so the awards were lost.
        """
        raw_stats = event.get("participantStatsByName")
        stats = raw_stats if isinstance(raw_stats, dict) else {}
        participants = event.get("participants")
        slots = [str(p) for p in participants] if isinstance(participants, list) else []

        by_player: dict[str, dict[str, int]] = {}
        for index, cid in enumerate(slots):
            player_stats: dict[str, int] = {}
            for name, column in stats.items():
                if isinstance(column, list) and index < len(column):
                    player_stats[str(name)] = _as_int(column[index])
            by_player[cid] = player_stats
        return Event(
            EventType.GAME_ROUND_END,
            data=by_player,
            extra={"awards": event.get("winnerByAward") or {}},
        )

    def _on_ping_summary(self, event: dict[str, Any]) -> Event | None:
        """``pingSummary`` — the only periodic statement of who is connected this engine makes."""
        raw = event.get("pingByPlayer")
        if isinstance(raw, dict):
            self._pings = {str(cid): _as_int(ping) for cid, ping in raw.items()}
        return None

    # -- console commands players run --------------------------------------

    def _on_console_command(self, event: dict[str, Any]) -> Event | None:
        """``consoleCommandExecute`` — a player ran a command in the game's own console.

        ``source`` is a vapor id, not a slot, and the all-zero one means the server itself ran it —
        which includes every command *this bot* sends, so reading those back would have the bot
        reacting to its own kicks.
        """
        source = str(event.get("source") or "")
        if not source or source == NOBODY:
            return None
        client = self._by_guid(source)
        if client is None:
            log.debug("altitude: console command from an unknown vapor id %s", source)
            return None
        command = str(event.get("command") or "")
        handler = self._commands.get(command)
        if handler is not None:
            return handler(event, client)
        return Event(
            EventType.CUSTOM,
            data=command,
            client=client,
            extra={"kind": "console_command", "arguments": event.get("arguments") or []},
        )

    def _on_call_vote(self, event: dict[str, Any], client: Client) -> Event | None:
        """``vote`` — arguments are the vote and its subject, e.g. ``["kick", "Courgette"]``."""
        arguments = event.get("arguments")
        args = [str(a) for a in arguments] if isinstance(arguments, list) else []
        return Event(
            EventType.CLIENT_CALLVOTE,
            data=" ".join(args),
            client=client,
            extra={"arguments": args},
        )

    def _on_cast_ballot(self, event: dict[str, Any], client: Client) -> Event | None:
        """``castBallot`` — arguments are the choice, e.g. ``["1"]``."""
        arguments = event.get("arguments")
        args = [str(a) for a in arguments] if isinstance(arguments, list) else []
        return Event(EventType.CLIENT_VOTE, data=args[0] if args else "", client=client)

    def _by_guid(self, guid: str) -> Client | None:
        for client in self.clients.connected():
            if client.guid and client.guid.lower() == guid.lower():
                return client
        return None

    # -- what the bot cannot ask the server --------------------------------

    def get_players(self) -> list[PlayerInfo]:
        """The roster, from the log rather than from a query — there is no query on this engine.

        Read by :meth:`b3.runtime.bot.Bot.get_players` through the same duck-typed seam Frostbite's
        client uses, and it exists mainly so `!status` and ping-watching have something to work with.

        **It reports the players this bot knows about, never fewer.** The temptation is to answer
        with the last ``pingSummary`` — that is what the classic parser did, and its `sync` then
        disconnected anyone missing from it. But a summary is a periodic snapshot: a player who
        joined a second ago is legitimately absent from it, so that rule silently forgets real
        players and loses the level and ban state attached to them. Nothing here is authoritative
        enough to *drop* anyone, and it does not need to be: this engine reports every departure as
        ``clientRemove``, which is where the roster shrinks.
        """
        players: list[PlayerInfo] = []
        pings = self._pings or {}
        for client in self.clients.connected():
            if client.cid is None:
                continue
            players.append(
                PlayerInfo(
                    cid=client.cid,
                    name=client.name,
                    guid=client.guid,
                    ip=client.ip,
                    ping=pings.get(client.cid, 0),
                )
            )
        unknown = set(pings) - {p.cid for p in players}
        if unknown:
            # Players who were already flying when the bot started. There is nothing to ask, so they
            # stay unknown until they do something the log names them in.
            log.debug("altitude: %d player(s) in the ping report are not in the roster", len(unknown))
        return players


def _identity(vapor_id: object) -> str:
    """A vapor id as an identity, which the all-zero one is not: it is every bot, and the server."""
    value = str(vapor_id or "")
    return "" if value == NOBODY else value


def _same_player(client: Client, vapor_id: str) -> bool:
    """Whether ``vapor_id`` is the player already recorded in that slot.

    Deliberately answers True when either side has no id. That means "cannot tell them apart", and
    the safe reading of that is "same player": claiming a difference invents a disconnect for
    somebody who never left, and both of the callers here would then act on it.
    """
    if not vapor_id or not client.guid:
        return True
    return client.guid.lower() == vapor_id.lower()


def _as_int(value: object, default: int = 0) -> int:
    """A number out of JSON, tolerating a string or a float — or absent."""
    if value is None:
        return default
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _position(event: dict[str, Any]) -> dict[str, Any]:
    """The (x, y) a powerup event carries, as event context."""
    return {"position": (event.get("positionX"), event.get("positionY"))}


__all__ = ["NOBODY", "REASON_LEFT", "REASON_VOTE_KICK", "AltParser", "KillData"]
