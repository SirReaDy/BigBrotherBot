| [Overview](../README.md) | [CLI](cli.md) | **Plugins** | [Deployment](deployment.md) | [Commands](commands.md) | [Configuration](configuration.md) | [Games](games.md) | [Development](development.md) |
|---|---|---|---|---|---|---|---|

# Plugins

## Bundled plugins

`admin` is always there. These are ported from the classic tree and switched on in the `plugins:`
list, each with an optional config of its own (see `examples/`):

| Plugin | What it does | Adds |
|---|---|---|
| `admin` | the 59 commands, groups, warnings | — |
| `censor` | bad language in chat, bad player names | — |
| `spamcontrol` | scores chat and warns whoever floods it | `!spamins [player]` |
| `pingwatch` | removes players whose connection is spoiling the game | `!ci <player>` |
| `tk` | team damage points, forgiving, and a ban for whoever will not stop | `!forgive` `!fp` `!forgiveall` `!forgivelist` `!forgiveinfo` `!forgiveclear` `!grudge` |
| `stats` | kills, deaths, damage, a relative skill score and XP, per session | `!mapstats` `!stats` `!testscore` `!topstats` `!topxp` |
| `welcome` | greets arrivals with their history, and announces their own greeting | `!greeting [text\|none]` |
| `afk` | asks players who look absent whether they are, and removes the silent ones | — |
| `banlist` | applies ban lists from a file or a URL, with whitelists that win | `!banlistinfo` `!banlistupdate` `!banlistcheck` |
| `cmdmanager` | command levels, aliases and per-player grants, at runtime | `!cmdlevel` `!cmdalias` `!cmdgrant` `!cmdrevoke` `!cmduse` |
| `customcommands` | commands you define in config, sending rcon lines of your own | whatever you name |
| `status` | writes the roster and server state to a file (or FTP, or tables) | — |
| `login` | admins must type a password before their level does anything | `!login` `!setpassword` |
| `ipban` | kicks a connecting player whose address is behind an active ban | — |
| `nickreg` | reserves nicknames, and warns whoever else is wearing one | `!registernick` `!deletenick` `!listnick` |
| `makeroom` | frees a slot on a full server, and holds it for a member | `!makeroom` `!makeroomauto` |
| `callvote` | decides who may call a vote, and records the ones that finish | `!veto` `!lastvote` |
| `spree` | announces killing and losing streaks | `!spree [player]` |

### tk — team damage

Hurting a teammate earns points **against that teammate**, scaled by the attacker's level: a kill by
an unregistered player is 200 points, and shooting into the spawn in the first few seconds costs
triple. Only the teammate can clear them — `!forgive`, or `!fp` for whoever hit you last — so nobody
is punished for something that has already been forgiven. Collect `max_points` and the server says so
publicly, giving anybody 30 seconds to forgive; collect half again as much and the ban is immediate,
for as long as the level's `ban_minutes` says, multiplied by how many teammates were hurt. Everyone's
points halve when a round or a map ends, so a bad round does not follow a player around.

`!grudge <player>` is the refusal to forgive: `!forgiveall` and `!fp` skip anybody grudged. The
levels table also decides who is exempt — nobody above its top entry is scored at all.

Team damage has to be reported by the engine, and four families state no damage *figure* (Source,
Frostbite, Homefront, Ravaged): a kill there is scored as the 100 damage a kill is, and hits of
unknown size are not scored at all rather than guessed at. Config: `examples/plugin_tk.yaml`.

### stats — the session's figures

`!mapstats` (or `!stats`) answers with a player's kills, deaths, team kills, damage, skill score and
XP for the session; `!testscore <player>` says what killing somebody would be worth before you try.
The skill score is **relative**, which is the point of it: killing a player who is doing well is worth
more than killing one who is not, so farming the weakest opponent on the server climbs slowly. Two
players on equal scores are worth 12.5 to each other; a victim on twice your score is worth 20.

`!topstats` and `!topxp` are the boards, top five, for regulars and up by default — and they list only
players who have actually done something, where the classic ranked anyone its scoring code had merely
read. Set `show_awards` to announce them at the end of each map.

Nothing is stored: these are figures about the session, and a player who reconnects starts again.
Lasting statistics were the classic bot's `xlrstats`, which is a project of its own rather than a
plugin. Config: `examples/plugin_stats.yaml`.

### welcome — the first thing a new player sees

Three messages sent privately — first visit, returning-and-unregistered, returning-and-registered —
and two announced to the server, thirty seconds after the player connects so the greeting lands after
the map has loaded. It matters more than it sounds: the first-visit message is where a new player
learns the server has a bot at all, and the returning one is where an unregistered player is told
about `!register`. A player past `newb_connections` visits is greeted privately but no longer
announced, and nobody is greeted twice inside `min_gap`.

Nothing is said for the first five minutes after the bot starts, because a bot restarted mid-match
authenticates the whole server at once. Somebody who leaves before their greeting is due does not get
one — nor does the next player to take their slot.

`!greeting <text>` (mod by default) sets a line announced when its owner joins; it may use `{name}`,
`{group}`, `{level}` and `{connections}`, and a placeholder that does not exist is refused when it is
typed rather than failing later in front of the server. `!greeting none` clears it. Wording for every
message lives in the main config's `messages:` section. Config: `examples/plugin_welcome.yaml`.

### afk — the slot somebody stopped using

Nothing is swept on a timer, which is the whole design: a player is checked only when there is a
reason to think they are away — they have died `consecutive_deaths` times in a row without doing
anything, or another player has said "afk" in chat (the word is the trigger, because "bob is afk" is
what people actually type; sweeps from it are throttled to one every fifteen seconds). A suspect is
asked privately, the server is told they have `last_chance_delay` seconds, and anything they do calls
it off.

Nobody is asked who cannot answer or should not be: bots, spectators, players at or above
`immunity_level`, and anybody the bot has not yet seen do anything at all — which is what makes a
fresh join, a map change and a bot restart safe without a special case for each. A round or map change
clears every record, so the slowest computer on the server is not the first thing this kicks. And no
kick ever takes the server below `min_ingame_humans` playing people, re-counted at the moment it is
due. Config: `examples/plugin_afk.yaml`.

### banlist — somebody else's ban list

Communities share ban lists, and this honours one without your admins retyping it. Four kinds: `ip`
(with the published `.0` range convention — an entry ending `.0` covers the last octet, `.0.0` two,
`.0.0.0` three), `guid`, `pbid` (a PunkBuster id, which is a different thing from a guid) and `roc`
("Rules of Combat" files). A list can be a local file, a URL cached into one, or both; URLs refresh
hourly with conditional requests, so a list nobody changed costs one round trip and no bytes.

Whitelists are checked first and win outright — that is the promise worth having, because it means a
list you do not control can never remove one of your own regulars. A player at or above
`immunity_level` gets an admin note rather than a kick.

Lists are checked at authentication **and when a player's address turns up**, which on the Call of Duty
and Quake 3 engines is not the same moment: their log lines carry no IP, and the status poll resolves
one seconds later. An IP list checked only at auth matches nobody at all on those titles.
Config: `examples/plugin_banlist.yaml`.

### cmdmanager — who may run what, without a restart

`!cmdlevel <command> [<level>]` moves a command; a level is a group keyword or a number, and
`mod-admin` sets a ceiling as well as a floor. `!cmdalias` gives it a shorter name, refusing one that
already belongs to another command. `!cmdgrant <player> <command>` is the one nothing else can do: it
gives **one player one command** without promoting them into a group that carries everything else with
it. `!cmdrevoke` takes it back and `!cmduse` answers "can they?" for anybody.

An admin can never change a command they cannot run themselves. Everything set this way lives in the
plugin's own tables, so it survives a restart *and* leaves your config files alone — the classic wrote
the new level back into the owning plugin's `.ini`, comments and all. Overrides are re-applied when a
plugin is enabled again, which would otherwise reset its commands to the levels in their code.
Config: `examples/plugin_cmdmanager.yaml` (documentation only — there is nothing to set).

### customcommands — your own commands, from config

Write a name, a level and the rcon line to send, and the bot registers a command like any other. The
line may carry placeholders: `<ARG>` (or `<ARG:OPT:default>`) for what the player typed,
`<ARG:FIND_PLAYER:PID>` for a player resolved the way every other command resolves one — also
`GUID`, `PBID`, `NAME`, `B3ID` — `<ARG:FIND_MAP>` for a map matched against the rotation, and
`<PLAYER:...>`, `<LAST_KILLER:...>`, `<LAST_VICTIM:...>` for the caller, whoever last killed them and
whoever they last killed. One argument per command, since that is all a command has.

**Everything substituted is sanitised.** The classic pasted a player's name into the rcon line
untouched, so a player called `bob"; quit` could turn an admin's `!slap` into two commands. A
placeholder this bot does not recognise is also refused when the command is *loaded* rather than sent
to the server as literal text — the classic's version of that was a command which looked configured
and did nothing on every use. Config: `examples/plugin_customcommands.yaml`.

### status — the roster, where a web page can read it

Every minute it writes what is happening — map, gametype, player count, and every player with their
level, score, ping and team — to a file, an FTP destination, or two tables of its own. The bot is the
only thing that knows all of that at once, which is why communities put it on their site.

JSON by default; `format: xml` writes the classic bot's own element and attribute names, so a status
page written for it keeps working. Uploads run on a worker thread, so a slow FTP server cannot stall
the bot, and credentials in the URL never reach the log. Two deliberate differences from the classic: a
masked admin is masked here too (this file is usually public, and that is what `!mask` is for), and
player IP addresses are left out unless `include_ip` asks for them. On shutdown the last write says the
server is empty — without it a page shows the players who were here when the bot stopped.
Config: `examples/plugin_status.yaml`.

### login — proving an admin is that admin

On most of these engines a player's identity is a guid their own client sends, and some titles hand out
none at all — so a spoofed identity is an admin account. With this plugin anybody above
`threshold_level` is a regular from the moment they connect until they type `!login <password>`, in the
game's private-message console rather than in chat. `!setpassword` sets your own, or that of somebody
**below** you: setting a password is taking an account.

Passwords are stored as a PBKDF2 hash with a per-password salt, where the classic stored a bare MD5.
An imported classic database keeps working — a 32-character digest is recognised, verified as a legacy
hash, and replaced with a modern one the first time its owner logs in — but the wider column needs
**migration 0003**, so run `b3 db upgrade` before enabling it on an existing database. Three wrong
passwords stop the guessing for a minute, which the classic did not do at all.
Config: `examples/plugin_login.yaml`.

### ipban — the ban a new guid does not escape

A ban is enforced on a guid, and a guid is evaded by reinstalling. This kicks anybody connecting from an
address that is behind an active ban or tempban — checked at authentication and again the moment an
address becomes known, since on Call of Duty and Quake 3 the log line that authenticates a player
carries no address. There is no list to maintain: the addresses come from the bans already issued, so
lifting a ban stops keeping that address out. Players above `max_level` (a registered user, by default)
are exempt, because a household address shared with somebody banned is the case this exists not to
punish. Config: `examples/plugin_ipban.yaml`.

### nickreg — the name that belongs to somebody

Impersonating an admin is the cheapest trick on a game server. `!registernick` reserves the name you
are using; anybody else wearing it is warned to change it, on a name change and on a sweep of the
connected players. `!listnick` shows what is reserved and `!deletenick` drops a registration — your own,
or anybody's with `manage_level`, and never one belonging to a player above you.

Names are compared as players see them: colour codes stripped, trimmed, lower-cased, so `^1Adm^7in` and
`admin` are the same nickname. Registrations live in the plugin's own table and outlive the session,
which is the point — the trick is played when the owner is away. Nothing is checked for a moment after a
map change, because a player still loading has whatever name the engine gave them.
Config: `examples/plugin_nickreg.yaml`.

### makeroom — a slot for the people who pay for the server

On a popular server the members are the ones who cannot get in. `!makeroom` (alias `!mkr`) announces
why, kicks the newest arrival from the lowest group, and holds the slot for `retain_free_duration`
seconds: a non-member who takes it in the meantime is kicked too, and a member arriving ends the window,
which is the whole point of it. `!makeroomauto on|off` (alias `!mrauto`) does the same continuously,
keeping `min_free_slots` free — off by default, because that setting is a door policy rather than
moderation. The slot count comes from the server (`sv_maxclients`); `total_slots` in the config is for
stating a smaller number than the truth, which is what slots reserved outside the bot look like.

It removes players who have done nothing wrong, so what it refuses matters more than what it does. Never
a bot (the engine refills it, so nothing is freed), never an admin wearing a mask (a mask hides rank, it
does not remove it), never the admin who asked, and — unless you set `only_when_full: no` — never on a
server that still has a free slot. "Newest" means the newest *arrival*, not the newest database record.
Config: `examples/plugin_makeroom.yaml`.

### callvote — the one power a player has over everybody

`callvote kick` on whoever is winning, `callvote map` to whatever empties the server. Each vote type
gets a level in the config, a vote from below it is cancelled with a private word to the player, and a
`maps:` table can set a level per map — which *replaces* the level for `map`, so you can lock the type
down and still leave the two maps that fill the server open to everybody. A type nobody has listed uses
`default_level`. `!lastvote` reports the last finished vote on this server, and `!veto` cancels the one
running.

Two things it will not do. Below `min_voters` humans nothing is checked at all, because a vote with one
voter has already passed by the time the log line reporting it is read — and bots are not voters. And
**cancelling a vote is engine-specific**: Urban Terror 4.2/4.3 have the verb, Homefront and Altitude
announce a vote and take no instruction about it. On those two the plugin says so once at startup and
then only records and announces, rather than telling a player they may not do what is about to happen
anyway. Config: `examples/plugin_callvote.yaml`.

### spree — the server saying a player's name

Five kills without dying gets a line to the whole server, and whoever ends it gets the credit; seven
deaths in a row gets a word of encouragement. Both tables are the operator's, keyed by the count, with a
`start` and an `end` message and `{player}` / `{victim}` in them — a placeholder this plugin cannot fill
is refused when the config is read rather than sent to the server as written.

A streak is kills *without dying*, so every kind of death breaks it: shot, team-killed, or walked off a
ledge. Team-killing does not build one. A death nobody caused ends a spree in silence, because the `end`
message names who did it and there is nobody. Counts reset at the end of a map, or per round if you ask
for it, or never. `!spree [player]` reports the streak somebody is on.
Config: `examples/plugin_spree.yaml`.

Anything else is a git install — see [Plugins](cli.md#plugins) and
[Writing an installable plugin](#writing-an-installable-plugin).

## Writing an installable plugin

A repo becomes installable by adding `b3plugin.yaml` at its root:

```yaml
name: chatlogger                            # the plugin's identity
version: 1.2.0
entry_point: b3_chatlogger:ChatLoggerPlugin  # dotted path, importable after install
min_core_version: 2.0.0                      # refuses to install on an older core
requires_plugins: [admin]                    # must be installed or bundled
config_template: conf/chatlogger.yaml        # optional; copied next to the main config
description: logs all chat to the database
homepage: https://github.com/owner/repo
```

The plugin itself subclasses `Plugin` and gets everything through `self.console` (the port) — never
by importing runtime internals:

```python
from b3.core.commands import CommandContext, command
from b3.core.events import EventType
from b3.core.plugin import Plugin

MESSAGES = {"chatlog_empty": "no chat recorded yet"}  # operators can reword these


class ChatLoggerPlugin(Plugin):
    requires_plugins = ("admin",)

    def on_load_config(self):
        self.log_chat = (self.config or {}).get("general", {}).get("log_chat", True)

    def on_startup(self):
        self.register_messages(MESSAGES)  # your text, still customisable
        Base.metadata.create_all(self.storage_engine())  # your own tables
        self.subscribe(EventType.CLIENT_SAY, self.on_chat)  # gated on the plugin being enabled
        self.schedule(self.prune, hour=4)  # cron work, torn down on unload

    @command(level=20, alias="cl")
    def cmd_chatlog(self, ctx: CommandContext):
        """chatlog [player] - show the last few chat lines"""
        ctx.reply(self.message("chatlog_empty"))
```

What the API gives you:

| Need | How |
|---|---|
| React to what happens | `self.subscribe(EventType.X, handler)` — 43 event types, including `ADMIN_COMMAND` for auditing what admins did |
| Add commands | `@command(level=, alias=)` on a method; the core registry handles parsing and permissions |
| Timed work | `self.schedule(handler, hour=4)` — crontab fields, removed when your plugin unloads |
| Your own tables | `self.storage_engine()` → declare SQLAlchemy models and `create_all` them. Keep to your own tables: the core's are Alembic-managed |
| Players, penalties, groups | `self.console.storage` — a typed contract, not raw SQL |
| Talk to the server | `self.console.say / tell / say_big / kick / ban / get_players / get_cvar / change_map` |
| Your own text | `self.register_messages({...})`, then `self.message("key", **values)`; an operator can override any of it from `messages:` in the main config |
| Your own settings | `self.config` — whatever your `config_template` YAML holds |

Everything a plugin touches is on the `Console` port or the `Plugin` base, so a plugin can be
tested against a fake console without a game server or a database.
