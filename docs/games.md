| [Overview](../README.md) | [CLI](cli.md) | [Plugins](plugins.md) | [Deployment](deployment.md) | [Commands](commands.md) | [Configuration](configuration.md) | **Games** | [Development](development.md) |
|---|---|---|---|---|---|---|---|

# Games, engine by engine

Which titles are supported at all is the table in
[supported games](../README.md#supported-games). This page is what each engine needs from you:
how it is reached, what it reports, and where it differs from the rest.

## Battlefield / Medal of Honor (Frostbite)

`bfbc2` `moh` (Frostbite 1) and `bf3` `bf4` `bfh` `mohw` (Frostbite 2). Like Arma, **there is no game
log**: the RCON connection carries the events too. Unlike every other game here, it is a **binary TCP**
protocol rather than text.

```yaml
server:
  game: bf3
  host: 127.0.0.1
  port: 47200            # the rcon port from the server's -remoteAdmin / admin.port setting,
                         # NOT the game port, and often not forwarded
  rcon_password: "…"     # admin.password from the server's startup arguments
```

- **`server.game_log` is ignored** — there is nothing to point it at, and `b3 doctor` says so instead
  of failing a check you cannot pass.
- **The password is never sent.** Frostbite uses a challenge-response login, so what crosses the wire
  is a hash of a server-supplied salt.
- **Events are opt-in**, and the bot asks for them on connect. If a bot ever seems connected but
  completely silent on one of these games, that is the request to look at — `b3 doctor` checks it.

What you get: identity from the EA GUID (which arrives after the connection, so a player is
authenticated a moment later than on other games), chat with squad/team/private channels distinguished,
headshot detection on kills, native GUID bans that live in the **server's own ban list** — so they hold
even while the bot is down, and across a player renaming — and `!unban` in one command.

Two limits worth knowing, both the protocol's. **A reason longer than 80 characters is rejected
outright**, so the bot caps it — a truncated reason beats no ban at all. And **Frostbite never reports
a player's IP address**, so IP history and IP-based lookups stay empty on these titles; PunkBuster is
the only source for that, and its messages currently reach plugins unparsed.

## Arma 2 / Arma 3 (BattlEye)

Different from every other game here in one way that matters operationally: **Arma has no game log for
the bot to read.** BattlEye's RCON connection carries the events as well as the commands, so:

- **`server.port` is BattlEye's port, not the game's.** It comes from `RConPort` in `beserver.cfg`
  (commonly 2302–2306, and *not* the port players connect on). Getting this wrong looks exactly like a
  wrong password, which is why `b3 doctor` tells the two apart for you.
- **`server.rcon_password` is `RConPassword` from `beserver.cfg`.**
- **`server.game_log` is ignored.** There is nothing to point it at; `b3 doctor` says so rather than
  failing a check you cannot pass.

```yaml
server:
  game: arma3
  host: 127.0.0.1
  port: 2306              # BePath/beserver.cfg RConPort — not the game port
  rcon_password: "…"      # beserver.cfg RConPassword
```

What you get: identity from the BattlEye GUID (verified by BattlEye's own backend, not merely claimed
by the client), chat on every channel with squad/side/vehicle/command treated as team chat, a native
timed ban the server enforces itself, and BattlEye's own kicks reported as what they are — the
anti-cheat's decision, kept out of your bot's penalty history.

Two behaviours worth knowing. Chat is **paced at 0.8s per line**, because an Arma server silently
drops a burst of `say` commands, so a long reply arrives over a few seconds. And because this protocol
has no goodbye packet, the bot **detects a vanished server and reconnects** with a back-off (5s
doubling to 5 minutes) — so a mission change or a nightly restart is a gap in the log, not the end of
the bot's evening. It works out whether the server is really gone by asking it something, since an
Arma server with nobody on it is silent too.

`!unban` does reach the server: BattlEye's `removeBan` takes a ban-list row number rather than a
player id, so the bot reads the ban list, matches the GUID, removes that row, and re-reads to confirm.
If it cannot confirm, it says so rather than reporting a success it did not verify.

## Altitude

The odd one out: Altitude has **no admin port at all**. The server writes a log of JSON events, and it
*reads* commands from a second file — so that file is the bot's RCON, and `server.command_file` is not
optional.

```yaml
server:
  game: altitude
  port: 27276                        # the game server's port: it identifies whose lines are whose
  game_log: "…/servers/log.txt"      # one JSON object per line
  command_file: "…/servers/command.txt"   # the file the server reads commands from
  encoding: utf-8                    # its log is JSON, so utf-8 rather than the CoD default
```

- **`server.rcon_password` is not used.** Write access to the command file is the authorisation. `b3
  doctor` checks that access by really writing, rather than trusting permission bits.
- **`server.port` matters even though nothing connects to it.** One Altitude installation runs several
  servers through *one* log and *one* command file, so every line carries a port: the bot ignores log
  lines from its neighbours and stamps its own commands so only its own server runs them.
- **Nothing can be asked of the server.** There is no reply channel, so the player list, the current
  map and everything else come from the log. `!maprotate` has no verb behind it on this engine and says
  so instead of quietly doing nothing; `!map <name>` works.

What you get: identity from the player's `vaporId` — which arrives *with* the connection, so a banned
player is recognised before they fly rather than on their first spawn — permanent and timed bans that
live in the server's own ban list, `!unban` in one command, and the game's own team colours, votes,
ball goals, powerups and end-of-round stats as events.

One behaviour is worth knowing because it is a safety measure rather than a convenience: **the bot
empties the command file when it starts and when it stops.** An Altitude server never truncates that
file — it remembers how far it has read — so anything left in it is executed again the next time the
server starts. A leftover `kick` or ban from a previous run would otherwise land on whoever holds that
name later. Bans are recorded in the bot's own database regardless, and re-applied on reconnect, so
nothing is lost by discarding the file.

## Homefront

Its admin protocol is its own thing: **one TCP connection carries commands and events**, like Arma and
Battlefield, so there is no game log to point at.

```yaml
server:
  game: homefront
  host: 127.0.0.1
  port: 27500            # the admin port from the server's configuration, not the game port
  rcon_password: "…"     # the admin password; only its SHA1 hash crosses the wire
```

- **`server.game_log` is ignored** — the connection is the event stream, and `b3 doctor` says so rather
  than failing a check you cannot pass.
- **The password is never sent.** The bot sends a SHA1 hash of it, uppercase and in space-separated
  pairs, which is the format this game insists on.
- **The server hangs up after ten seconds of silence**, so the bot pings well inside that. A connection
  that opens and then dies is a keepalive problem rather than a credentials one.

What you get: identity from the player's Steam id — so a rename is still the same person — chat with
team and squad channels distinguished, team-kill detection, votes, clan changes, and the server's own
ban list read back as the bot sees it.

Three limits worth knowing, all the engine's. There is **no timed ban**: it has kick and kickban and
nothing between, so a tempban is a kick the bot re-applies from its own record. There is **no way to
load a named map**, only "next", so `!maprotate` works and `!map <name>` does not. And **nothing can be
asked synchronously** — the player list arrives as pushed messages in answer to a request, so the bot
keeps asking every fifteen seconds and assembles the roster from the replies.

## Ravaged

One TCP connection again, and a protocol with a quirk worth stating: **the two directions have
different shapes.** The bot writes a plain line; the server answers with a length in brackets.

```yaml
server:
  game: ravaged
  host: 127.0.0.1
  port: 13550            # the admin port from the server's configuration, not the game port
  rcon_password: "..."   # the admin password; only its SHA1 hash crosses the wire
```

- **`server.game_log` is ignored** — the connection is the event stream, and `b3 doctor` says so rather
  than failing a check you cannot pass.
- **Too many wrong passwords get your address blacklisted**, not merely refused — so the bot tries once
  and stops, and `b3 doctor` tells you which of the two has happened. If you are blacklisted, fix the
  password and wait for the server to forget before connecting again.
- **Bans are counted in days.** The bot converts whatever duration you give it, so `!tempban bob 2h`
  works; a permanent ban goes out as a year and is held permanently in the bot's own database.
- **Everything is keyed on the Steam id**, so a rename is still the same person and a ban follows them.

What you get: kills with the damage type, team-kill detection, chat with team chat distinguished,
round starts and results, the rotation and `!map`, and scores and pings — which appear in no log line
on this engine, so the bot asks for the player list every fifteen seconds to keep them current.

## Frontlines: Fuel of War

One TCP connection again, and the only game here whose login needs a **user name as well as a
password**.

```yaml
server:
  game: frontline
  host: 127.0.0.1
  port: 14507            # the remote console port from the server's configuration, not the game port
  rcon_user: admin       # the admin account name -- this engine authenticates one
  rcon_password: "..."   # only an MD5 hash of it, salted by the server's challenge, crosses the wire
```

- **`server.game_log` is ignored** — the connection is the event stream, and `b3 doctor` says so rather
  than failing a check you cannot pass.
- **A wrong user and a wrong password look identical**: this server refuses a login by hanging up
  without a word. `b3 doctor` reports it as a refusal rather than a network problem, and names both
  settings, because the server will not say which one it was.
- **Bans are keyed on the player's ProfileID**, their account, so a ban follows them across a rename and
  across the slot number changing. Durations are in minutes; a permanent ban is `BanTime=0`.
- **The bot switches the server's reporting on** when it connects, and again after a restart. Without
  that this connection is silent — no chat and no debug lines at all.

What you get: chat with team and squad channels distinguished, joins and departures, team changes,
scores and pings, the rotation and `!map`, the server's own ban list changes (including bans an admin
made at the console), and IP addresses when PunkBuster logging is on.

One property of this engine is worth knowing because it sets the pace of everything: **it reports no
connect or disconnect lines at all.** The player list is the only statement of who is playing, so the
bot asks for it every three seconds and works out the joins and departures from the difference. That is
why arrivals are noticed within a few seconds rather than instantly.

## Insurgency (Source)

The first Source title here, and the only family that reads a log file *and* logs in to a stateful RCON
session. Events come from `console.log`; commands go over Source RCON, which shares the game's port over
**TCP**.

```yaml
server:
  game: insurgency
  host: 127.0.0.1
  port: 27015                       # the game port -- rcon shares it, over TCP
  rcon_password: "..."              # the server's rcon_password
  game_log: /srv/insurgency/insurgency/console.log
```

Two things to set up on the game server, and neither is optional:

- **Start the server with `-condebug`**, which is what makes it write `console.log` at all. Without it
  there is no log to point `server.game_log` at.
- **Install [SourceMod](https://www.sourcemod.net/).** A stock Source server has no command for a
  private message and none for a ban that survives a restart, so on this engine every command reply,
  every `!` answer and the whole of `!ban` go through SourceMod. The bot checks for it on connect and
  **refuses to start without it**, rather than running as a bot whose `!ban` quietly does nothing;
  `b3 doctor` reports it as its own line, separately from whether the password worked.

Worth knowing:

- **Choose different command prefixes from SourceMod's.** SourceMod also answers `!` commands; see
  `PublicChatTrigger` and `SilentChatTrigger` in `addons/sourcemod/configs/core.cfg`.
- **Everything is keyed on the Steam id**, so a rename is still the same person and a ban follows them.
  Both `STEAM_1:0:...` and the newer `[U:1:...]` form are read as identities.
- **A ban is two commands** — one to write the ban list, one to remove the player who is on the server
  now. The first alone leaves a banned player in the game until the map ends.
- **AI players appear as players but are never given an identity.** Every bot on a Source server reports
  the same `BOT`, so identifying them would pile every AI that ever played into one database record with
  a single level and ban history between them.
- **`!maps` lists what is installed, not a rotation** — that is all `maps *` can answer. `!nextmap` reads
  SourceMod's `sm_nextmap` instead of guessing from that list, and says it does not know if the nextmap
  plugin is not loaded.

What you get: chat with team chat distinguished, joins, departures and team changes, kills with the
weapon and whether it was a headshot, assists, objective actions, round and match state, the map, the
roster with pings and addresses, and the server's own bans and kicks — including ones made at the
console by somebody else.

## Which CoD4 are you running?

Nearly every live CoD4 server runs **CoD4X 1.8** rather than the stock 1.7 binary, and they differ in
ways that matter. Set `server.game` accordingly:

`server.game` accepts every Call of Duty title the classic bot supported: `cod` (Call of Duty 1),
`cod2`, `cod4`, `cod4x`, `cod4gr` (GameRanger), `cod5`, `cod6` (MW2), `cod7` (Black Ops), `cod8`
(MW3). They share one parser — the differences are data. All of them are asked for `g_logsync` on
connect, without which the engine buffers its log file and the bot reacts late or not at all, and all
of them wrap chat at 65 characters, which is as much as a Call of Duty console will show.

Two caveats: **cod6/cod8 have no working ban verb**, so a ban there is a kick the bot re-applies on
every reconnect (as it was classically); and **cod7 logs** were served by Activision's long-dead
"Elite" service, so Black Ops needs a server that writes a log file the bot can reach. Its RCON
framing differs from the other titles and is handled, as does the way it takes cvars: Black Ops
refuses a plain `set` over RCON, so the bot uses `setadmindvar` there — including for the two
settings that decide whether its log is readable at all.

## Plutonium (MW3 and Black Ops 2)

`plutoiw5` is Modern Warfare 3 and `plutot6` is **Black Ops 2** on the [Plutonium](https://plutonium.pw/)
client. Same log grammar, same RCON, so they are two more titles rather than new machinery — but three
of their differences are the kind that fail silently, and each is handled:

- **Chat is cut short by the engine**, at 43 characters on IW5 and 72 on T6. The bot wraps to
  whichever is smaller, that or your `bot.line_length`; a config value can ask for shorter lines than
  the game shows but cannot lift the game's own limit.
- **Bots share one GUID** — `0` on T6, a long common prefix or `bot<N>` on IW5 — and both status
  tables also carry a `bot` column, which is believed over any of that. AI players still appear as
  players, but they are never given an identity, so they cannot pile into one database record with a
  shared level and ban history between them.
- **A bot's ping is not a number** (letters on IW5, blank on T6). Read as zero, an AI would look like
  the best-connected player on the server.
- **IW5 reads cvars differently** from every other title here: `get <name>`, answered as
  `name is "value"` rather than the Quake 3 form. T6 uses the Quake 3 form.

Neither title has a working ban verb, so as on MW2/MW3 a ban is a kick that the bot re-applies from
its own record when the player returns.

| | `cod4` (stock) | `cod4x` (CoD4X 1.8) |
|---|---|---|
| Identity | 32-character GUID | **Steam64 id** — the bot sends `sv_usesteam64id 1` on connect, and keys players on the Steam id rather than the per-session GUID beside it |
| Ban | `banclient` | `permban <slot> <reason>` |
| Timed ban | none — the bot re-applies it on reconnect | **native** `tempban <slot> <mins>m <reason>`, capped by the engine at 30 days |
| Unban | by name, connected players only | `unban <guid>` — reaches someone who has left |
| Reasons | not shown to the player | passed to the engine, so the player sees why |

Getting this wrong is loud, not silent: pointing `cod4x` at a server that is not reporting Steam64
ids makes the bot log *"ignoring guid … Nobody will be authenticated while this is the case"* rather
than quietly failing to recognise anybody. The player list is read whether or not that cvar is
honoured — a CoD4X server with `sv_usesteam64id` off prints one numeric id column instead of two, and
that shape is parsed too, rather than being mistaken for part of the player's name.

**On CoD4X the bot asks `b3status` before `status`.** Servers running the `b3hide` mod — the usual way
admins hide themselves — leave hidden players out of `status` altogether and answer `b3status` with the
whole table, so asking the fuller question first is what stops the bot losing sight of the very people
using it. A server without the mod does not know that command; the bot notices, falls back to `status`,
and remembers which one worked. Load the mod later and it finds `b3status` again without a restart.
If a table shape turns up that neither pattern fits, the bot says so and quotes the row, instead of
reporting an empty server.

A tempban longer than CoD4X allows is still recorded in full — the engine gets its 30-day maximum,
and the bot re-applies the remainder when the player next connects.
