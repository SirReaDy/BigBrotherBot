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
