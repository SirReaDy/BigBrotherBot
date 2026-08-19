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
