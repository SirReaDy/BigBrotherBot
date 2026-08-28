"""FbParser — the events a Frostbite server pushes, as word lists.

This is the first family whose grammar is **not** regex-shaped, so it is the first that does not use
the `@handles` router. A Frostbite event is a list of words whose first element names it:

    ["player.onChat", "Bravo17", "!ban Bob", "all"]
    ["player.onKill", "Bravo17", "Bob", "M67", "false"]
    ["player.onAuthenticated", "Bravo17", "EA_1234…"]
    ["player.onLeave", "Bob", "<player info block…>"]
    ["server.onLevelLoaded", "MP_001", "ConquestLarge0", "1", "2"]
    ["punkBuster.onMessage", "PunkBuster Server: …"]

Matching those with regexes would mean flattening them into text first and then splitting the text
back up — inventing an ambiguity (a word may contain spaces, or any other delimiter) purely to reuse
a mechanism that does not fit. So this parser dispatches on ``words[0]`` instead, through a plain
dict. The lines it is handed are JSON, which is how :mod:`b3.net.frostbite` gets a word list through
the line-shaped ``LogSource`` contract intact.

**Identity is the player's name**, uniquely among the families here. `admin.kickPlayer` takes a name,
not a slot, so the name *is* the handle, and it goes in ``cid``. The EA GUID arrives separately, on
``player.onAuthenticated``, and that is the join the rest of the bot acts on — before it there is no
identity to match a ban against.

**Squads and the end-of-round scoreboard are here**, and they are what the `poweradmin` plugins
needed: a player's squad on `player.onSquadChange` and in the roster block, and
`server.onRoundOverPlayers` / `server.onRoundOverTeamScores` as events. They arrive in the window
between a round ending and the next map loading, which is the only moment a team scrambler can be
fair rather than random.

Not implemented: the PunkBuster message grammar (a dozen regexes over ``punkBuster.onMessage`` text,
which belongs with PunkBuster support rather than here).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence

from b3.core.clients import ClientManager
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.net.frostbite import split_command
from b3.parsers.base import Parser
from b3.parsers.frostbite.status import parse_player_block
from b3.parsers.profile import GameProfile

log = logging.getLogger(__name__)

#: The name a Frostbite server uses for itself when it is the one talking, and for a kill with no
#: killer (an explosion, a fall). Never a real player.
SERVER_NAME = "Server"

#: Chat targets the server reports. "all" is public; the rest are restricted, so they map to team
#: chat — the same distinction the BattlEye parser draws, and for the same reason.
PUBLIC_TARGETS = frozenset({"all", ""})


class FbParser(Parser):
    """Battlefield and Medal of Honor. Selected by ``family="frostbite"`` in the profile."""

    def __init__(self, profile: GameProfile, clients: ClientManager | None = None) -> None:
        super().__init__(profile, clients)
        #: event name -> handler. Built once; see the module docstring for why not `@handles`.
        self._handlers: dict[str, Callable[[Sequence[str]], Event | None]] = {
            "player.onChat": self._on_chat,
            "player.onAuthenticated": self._on_authenticated,
            "player.onJoin": self._on_join,
            "player.onLeave": self._on_leave,
            "player.onKill": self._on_kill,
            "player.onSpawn": self._on_spawn,
            "player.onTeamChange": self._on_team_change,
            "player.onSquadChange": self._on_squad_change,
            "player.onKicked": self._on_kicked,
            "server.onLevelLoaded": self._on_level_loaded,
            # **Frostbite 1's spelling of the same two moments, and it is not the one above.** Bad
            # Company 2 and the 2010 Medal of Honor announce a map with `server.onLoadingLevel` and
            # start the round with `server.onLevelStarted`; Frostbite 2 replaced both with the
            # single `server.onLevelLoaded`. The classic wrote a parser per generation and each
            # handled its own pair, so nothing there had to notice — here one parser served both
            # and carried only Frostbite 2's, which meant those two titles produced **no map change
            # and no round start at all**. Every per-round reset in the bot — the scoreboard, sprees,
            # the team balancer, match mode, vote delay — was waiting for an event that never came.
            "server.onLoadingLevel": self._on_loading_level,
            "server.onLevelStarted": self._on_level_started,
            "server.onRoundOver": self._on_round_over,
            "server.onRoundOverPlayers": self._on_round_over_players,
            "server.onRoundOverTeamScores": self._on_round_over_team_scores,
            "punkBuster.onMessage": self._on_punkbuster,
        }

    #: Events this engine sends that nothing here reads — named rather than left as an omission, so
    #: the next person finds a decision instead of a gap.
    #:
    #: ``player.onDisconnect <name> <reason>`` is BF4's and Hardline's, and it arrives *alongside*
    #: `player.onLeave` for one departure. What it adds is the engine's own word for why —
    #: ``PLAYER_CONN_LOST``, or the text of the kick or ban that ended the session — and empty when
    #: the player simply left. Carrying it would mean a `CLIENT_DISCONNECT_REASON` event type that
    #: nothing subscribes to, which this project has declined before for the same reason (see the
    #: freeze-tag lines in TODO.md §1.3): an event nobody reads is indistinguishable from one that
    #: is wrong. Whoever wants it: add the type, publish it here, and do **not** remove the client —
    #: `player.onLeave` already does, and doing it twice drops whoever has taken the slot since.
    UNREAD_EVENTS = ("player.onDisconnect",)

    # -- dispatch ----------------------------------------------------------

    def parse_line(self, line: str) -> list[Event]:
        """Read one JSON word list and route it.

        Overrides the regex router entirely rather than working around it. An unknown event is
        ignored quietly: Battlefield servers emit plenty this bot has no use for, and warning about
        each would bury the log.
        """
        words = self._decode(line)
        if not words:
            return []
        handler = self._handlers.get(words[0])
        if handler is None:
            log.debug("frostbite: no handler for %s", words[0])
            return []
        event = handler(words[1:])
        return [event] if event is not None else []

    def handler_for(self, line: str) -> str | None:
        """Which handler would read this event — see :meth:`b3.parsers.base.Parser.handler_for`.

        Overridden because this family dispatches on ``words[0]`` rather than through the regex
        router.
        """
        words = self._decode(line)
        if not words:
            return None
        handler = self._handlers.get(words[0])
        return None if handler is None else handler.__name__

    @staticmethod
    def _decode(line: str) -> list[str]:
        line = line.strip()
        if not line:
            return []
        try:
            words = json.loads(line)
        except json.JSONDecodeError:
            # Not ours. A hand-written replay file of plain text would land here, and saying so once
            # is more useful than a stack trace per line.
            log.warning("frostbite: expected a JSON word list, got %r", line[:60])
            return []
        if not isinstance(words, list) or not all(isinstance(word, str) for word in words):
            log.warning("frostbite: expected a list of strings, got %r", words)
            return []
        return words

    # -- identity ----------------------------------------------------------

    def _client(self, name: str) -> Client | None:
        """The player called ``name``, creating the record if this is the first sight of them.

        ``Server`` is not a player: chat and kills attributed to it come from the game itself, and
        making a client out of it would put the server in the database with a level and a ban history.
        """
        name = name.strip()
        if not name or name == SERVER_NAME:
            return None
        client = self.clients.get_by_cid(name)
        if client is None:
            client = Client(cid=name, name=name)
            self.clients.add(client)
        return client

    def _on_authenticated(self, words: Sequence[str]) -> Event | None:
        """``player.onAuthenticated <name> <EA GUID>`` — the join the rest of the bot acts on."""
        client = self._client(words[0] if words else "")
        if client is None:
            return None
        if len(words) > 1 and words[1]:
            client.guid = words[1]
        client.authed = False  # so the runtime authenticates them against the database
        return Event(EventType.CLIENT_JOIN, client=client)

    def _on_join(self, words: Sequence[str]) -> Event | None:
        """``player.onJoin <name> [<EA GUID>]`` — earlier than authentication, and less trustworthy.

        The classic parser ignored this event outright, with a good reason recorded in its comments:
        it arrives before the game client has really connected, and a client that then fails to
        connect never produces a matching `onLeave` — leaving the bot convinced somebody is playing
        who is not. Reported as a connection, which is what it is, and nothing hangs off it.
        """
        client = self._client(words[0] if words else "")
        if client is None:
            return None
        return Event(EventType.CLIENT_CONNECT, client=client)

    def _on_leave(self, words: Sequence[str]) -> Event | None:
        """``player.onLeave <name> <player info block>``."""
        name = (words[0] if words else "").strip()
        client = self.clients.remove(name)
        if client is None:
            return None
        return Event(EventType.CLIENT_DISCONNECT, client=client)

    def _on_kicked(self, words: Sequence[str]) -> Event | None:
        """``player.onKicked <name> <reason>`` — including kicks this bot asked for."""
        name = (words[0] if words else "").strip()
        reason = words[1] if len(words) > 1 else ""
        client = self.clients.remove(name)
        if client is None:
            return None
        return Event(EventType.CLIENT_DISCONNECT, client=client, data=reason)

    # -- chat --------------------------------------------------------------

    def _on_chat(self, words: Sequence[str]) -> Event | None:
        """``player.onChat <name> <text> <target>``.

        The bot's own output comes back on this event with ``Server`` as the speaker, which is why
        that name is refused: reading it would feed the bot its own command replies.
        """
        if len(words) < 2:
            return None
        client = self._client(words[0])
        if client is None:
            return None
        target = words[2] if len(words) > 2 else "all"
        etype = (
            EventType.CLIENT_SAY
            if target.split(" ")[0] in PUBLIC_TARGETS
            else EventType.CLIENT_TEAM_SAY
        )
        return Event(etype, data=words[1], client=client, extra={"target": target})

    # -- combat ------------------------------------------------------------

    def _on_kill(self, words: Sequence[str]) -> Event | None:
        """``player.onKill <killer> <victim> <weapon> <headshot>``.

        An empty killer means the world did it — a fall, a vehicle explosion, the server killing
        somebody. That is a suicide as far as the bot's vocabulary goes, and it must not be blamed on
        a player called "".
        """
        if len(words) < 2:
            return None
        victim = self._client(words[1])
        if victim is None:
            return None
        weapon = words[2] if len(words) > 2 else ""
        headshot = len(words) > 3 and words[3].lower() == "true"
        data = KillData(weapon=weapon, headshot=headshot)

        killer_name = (words[0] or "").strip()
        if not killer_name or killer_name == SERVER_NAME or killer_name == victim.cid:
            return Event(EventType.CLIENT_SUICIDE, data=data, client=victim, target=victim)

        attacker = self._client(killer_name)
        if attacker is None:
            return None
        team_kill = (
            attacker.team is not None and attacker.team != "" and attacker.team == victim.team
        )
        etype = EventType.CLIENT_KILL_TEAM if team_kill else EventType.CLIENT_KILL
        return Event(etype, data=data, client=attacker, target=victim)

    def _on_spawn(self, words: Sequence[str]) -> Event | None:
        """``player.onSpawn <name> <team>`` on Frostbite 2; the kit on Frostbite 1."""
        client = self._client(words[0] if words else "")
        if client is None:
            return None
        if len(words) > 1:
            team = self.profile.teams.get(words[1], words[1])
            # Frostbite 1 reports the *kit* here rather than a team, so only take it when it looks
            # like one of this title's team ids. Guessing would relabel everyone as "assault".
            if words[1] in self.profile.teams:
                client.team = team
        return Event(EventType.CLIENT_SPAWN, client=client)

    def _on_team_change(self, words: Sequence[str]) -> Event | None:
        """``player.onTeamChange <name> <team> <squad>``.

        The squad is recorded as well as the team. It was dropped before, and it is not decoration on
        this engine: a Battlefield squad is four players who spawn on each other, so swapping two
        players between teams means putting each into the *other's* squad — with the squad unknown
        they both land in "no squad" and the swap is half done.
        """
        client = self._client(words[0] if words else "")
        if client is None or len(words) < 2:
            return None
        client.team = self.profile.teams.get(words[1], words[1])
        if len(words) > 2:
            client.squad = words[2]
        return Event(EventType.CLIENT_TEAM_CHANGE, data=client.team, client=client)

    def _on_squad_change(self, words: Sequence[str]) -> Event | None:
        """``player.onSquadChange <name> <team> <squad>`` — a team change often arrives as this."""
        return self._on_team_change(words)

    # -- the round ---------------------------------------------------------

    def _on_level_loaded(self, words: Sequence[str]) -> Event | None:
        """``server.onLevelLoaded <map> <gamemode> <rounds played> <rounds total>``.

        Published with the same cvar-style payload the other families produce for a round start, so
        `Game` state and any plugin watching for a new map work unchanged.
        """
        data = {
            "mapname": words[0] if words else "",
            "g_gametype": words[1] if len(words) > 1 else "",
        }
        if len(words) > 2:
            data["roundsPlayed"] = words[2]
        if len(words) > 3:
            data["roundsTotal"] = words[3]
        return Event(EventType.GAME_ROUND_START, data=data)

    def _on_loading_level(self, words: Sequence[str]) -> Event | None:
        """``server.onLoadingLevel <map> <rounds played> <rounds total>`` — Frostbite 1's map change.

        Three fields where the other generation sends four: this one has no gamemode column,
        because its map list keeps bare level names and nothing else (see the profiles module).
        The level arrives as a path — ``levels/mp_004`` — which the map tables already match by
        longest prefix.

        Published as a round start, the same as `server.onLevelLoaded` on Frostbite 2, so that a
        plugin does not have to know which generation it is running on. That both generations name
        the *load* rather than the start is a shared inaccuracy rather than a new one: the classic
        deferred its round start to the first spawn afterwards, and the difference is discussed with
        `_on_level_started` below.
        """
        data = {"mapname": words[0] if words else ""}
        if len(words) > 1:
            data["roundsPlayed"] = words[1]
        if len(words) > 2:
            data["roundsTotal"] = words[2]
        return Event(EventType.GAME_ROUND_START, data=data)

    def _on_level_started(self, _words: Sequence[str]) -> Event | None:
        """``server.onLevelStarted`` — Frostbite 1 only, and deliberately silent.

        This is the moment the round actually begins, after the warmup the load event announces, and
        it takes no arguments. It is *not* published, because `_on_loading_level` has already raised
        the round start for this map and a second one would run every per-round reset twice — the
        scoreboard cleared after it had been filled, a spree cancelled seconds after it started.

        Which of the two should carry the event is a real question and this is the conservative
        answer: the load event is the only one of the pair that names the map, and on Frostbite 2
        there is no `onLevelStarted` to move it to (the classic synthesised one from the first
        `player.onSpawn`). Handled by name rather than left to fall through so that it is a decision
        on the record instead of an event nobody noticed.
        """
        return None

    def _on_round_over(self, words: Sequence[str]) -> Event | None:
        """``server.onRoundOver <winning team>``."""
        return Event(EventType.GAME_ROUND_END, data=words[0] if words else "")

    def _on_round_over_players(self, words: Sequence[str]) -> Event | None:
        """``server.onRoundOverPlayers <player info block>`` — the final scoreboard.

        The same block shape the roster arrives in, so it is read by the same code. This is the one
        moment the bot is told what each player actually *did*, and it arrives after the round is
        over and before the next map loads — which is exactly the window a team scrambler has to work
        in. Without it a scrambler can only shuffle at random.
        """
        players = list(parse_player_block(list(words)))
        if not players:
            return None
        return Event(EventType.GAME_ROUND_PLAYER_SCORES, data=players)

    def _on_round_over_team_scores(self, words: Sequence[str]) -> Event | None:
        """``server.onRoundOverTeamScores <count> <score> ... <target score>``.

        A counted list, like everything else this protocol sends, with the target score tacked on
        the end. Published as the scores alone: which team won is `server.onRoundOver`'s business and
        is already an event of its own.
        """
        if not words or not words[0].isdigit():
            return None
        count = int(words[0])
        scores = list(words[1 : 1 + count])
        if len(scores) < count:
            log.warning("frostbite: team score list ended after %d of %d", len(scores), count)
            return None
        return Event(EventType.GAME_ROUND_TEAM_SCORES, data=scores)

    def read_server_info(self, reply: str) -> dict[str, str]:
        """Read a ``serverInfo`` reply — the classic ``getServerInfo``/``getServerVars``.

        This engine has no cvars, so this command is the only way the bot learns the server's name,
        its player limit, its gametype or how many rounds a map runs for. The reply is **positional**::

            <hostname> <players> <max players> <gamemode> <map> <rounds played> <rounds total> ...

        Returned under cvar names rather than as fields, so it merges through exactly the path every
        other family's values take (`Game.update_cvars`) instead of being a second way to set them.

        Read defensively by index: the two protocol generations carry different numbers of trailing
        fields and DICE added more over time, so anything past the ones named here is ignored rather
        than assumed absent.
        """
        words = split_command(reply)
        if not words:
            return {}
        named = ("sv_hostname", "", "sv_maxclients", "g_gametype", "mapname")
        info = {key: words[i] for i, key in enumerate(named) if key and i < len(words)}
        if len(words) > 5:
            info["roundsPlayed"] = words[5]
        if len(words) > 6:
            info["roundsTotal"] = words[6]
        return info

    def _on_punkbuster(self, words: Sequence[str]) -> Event | None:
        """``punkBuster.onMessage <text>``.

        Passed through rather than parsed: PunkBuster's message grammar is a dozen regexes and
        belongs with PunkBuster support. Publishing the raw text means a plugin can read it and the
        messages are not discarded.
        """
        return Event(EventType.CUSTOM, data=words[0] if words else "", extra={"kind": "punkbuster"})


class KillData:
    """Payload for a Frostbite kill.

    Not the CoD/Quake3 :class:`b3.parsers.cod.parser.KillData`: this engine reports no damage figure
    and no hit location, but it does say whether the shot was a headshot — so inventing a damage of
    100 to fit the other shape would be making data up.
    """

    __slots__ = ("headshot", "weapon")

    def __init__(self, weapon: str, headshot: bool = False) -> None:
        self.weapon = weapon
        self.headshot = headshot

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<KillData weapon={self.weapon!r} headshot={self.headshot}>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, KillData):
            return NotImplemented
        return (self.weapon, self.headshot) == (other.weapon, other.headshot)
