| [Overview](../README.md) | [CLI](cli.md) | [Plugins](plugins.md) | [Deployment](deployment.md) | [Commands](commands.md) | **Configuration** | [Games](games.md) | [Development](development.md) |
|---|---|---|---|---|---|---|---|

# Configuration

One typed YAML file, validated at load — a bad value fails immediately with a clear message.
Path values accept tokens: `@b3` (the installed package dir), `@conf` (the main config's directory),
`@home` (`~/.b3`), and `~`.

```yaml
bot:
  name: b3                            # default: b3
  prefix: "^2(b3)^7:"                 # in front of everything the bot says (colour codes allowed)
  pm_prefix: "^8[pm]^7"               # added on top of it for a private reply

  time_zone: UTC                      # IANA name; schedules are evaluated in this zone
  log_level: INFO
  line_length: 90                     # game chat limit; longer replies wrap across lines
  line_color_prefix: ""                # prepended to each continuation line, e.g. "^3"
  dead_prefix: "[DEAD]^7"             # marks a message sent only to players waiting to respawn
  database: "sqlite:///b3.sqlite"     # any SQLAlchemy URL (sqlite / mysql+pymysql / postgresql+psycopg)
  server_id: ""                       # names this server on the penalties it issues; only
                                      # needed when several bots share one database
  plugins_dir: "@home/plugins"        # where `b3 plugin install` puts things

  update_check: true                  # notice a new release: once a day while the bot runs, and at
                                      # most once a week after a command. false switches both off
  update_remote: "https://github.com/SirReaDy/BigBrotherBot.git"   # empty also switches it off
  update_check_interval: "24h"        # how long the running bot keeps an answer before asking again

server:
  game: cod4x                         # cod4x (the CoD4X 1.8 mod) or cod4 (stock 1.7)
  rcon_password: "changeme"
  host: 127.0.0.1
  port: 28960
  game_log: "games_mp.log"            # local path, or an ftp/sftp/http URL (see below)
  encoding: latin-1                   # CoD engines are latin-1
  rcon_timeout: 0.8
  rcon_user: admin                    # only Frontline and Ravaged authenticate an account name
  punkbuster:                         # unset asks the server; true insists and warns if absent;
                                      # false never asks. Call of Duty, Quake 3 and Frostbite only
  log_poll_interval: 2.0              # remote logs only: seconds between polls
  log_timeout: 10.0                   # remote logs only: per-operation network timeout
  log_max_gap: 20480                  # remote logs only: skip ahead past a bigger jump than this

plugins:
  - name: admin                       # -> b3.plugins.admin
  - name: chatlogger
    config: "@conf/plugin_chatlogger.yaml"   # the plugin's own settings
  - name: afk
    disabled: true                    # loaded but inert
  - name: mything
    module: "my_pkg.b3plugin:MyPlugin"  # code living outside the b3 tree

# Optional: override any built-in text. Anything you omit keeps its default.
messages:
  kicked: "^1{name}^7 was removed: {reason}"
  iamgod_done: "^2you are now superadmin^7"
```

## How plugins load

Which plugins run is decided entirely by the `plugins:` list — nothing is hardcoded.

- **Resolution.** `name` is the plugin's identity; it resolves to `b3.plugins.<name>` unless `module`
  says where the code lives (`pkg.mod` or `pkg.mod:ClassName`). `b3 plugin install` fills in `module`
  from the manifest's `entry_point`.
- **Order.** Dependency order, from each plugin's `requires_plugins` (hard) and `load_after` (soft)
  manifest, tie-broken by the order in your config — so loading is reproducible.
- **`disabled` means inert, not absent.** A disabled plugin is instantiated but never started: no
  commands, no event handlers. The same applies when its hard dependency is off, or its
  `requires_parsers` excludes your game. `Plugin.enable()` runs the deferred startup, so runtime
  toggling works.
- **Startup stops** on an operator mistake: unknown plugin, duplicate entry, missing hard dependency,
  dependency cycle, or a missing config file for a plugin that *will* run. Quietly running without a
  plugin you asked for is worse than refusing to start.

## Messages

Every piece of user-facing text is a named template with a built-in default, overridable from the
`messages:` block above — the modern equivalent of the classic bot's `[messages]` config section.
Keys cover the command framework (`unknown_command`, `insufficient_access`, …), moderation
(`kicked`, `banned`, `tempbanned`, `unbanned`, …), penalty and identity output, and `!help`/`!iamgod`.
The full list with its defaults is `DEFAULT_MESSAGES` in `src/b3/core/messages.py`.

Placeholders use `{name}` style. A template that references a placeholder the bot does not supply is
logged and sent verbatim rather than raising, so a typo in your config can never take a command down;
a key the bot never uses is flagged at startup.

Replies longer than `line_length` are word-wrapped across several lines (a word longer than the limit
is broken rather than dropped), and an embedded `\n` starts a new line. Without this the game
truncates long output mid-word.

**The prefixes count towards that limit.** `prefix` goes in front of everything the bot says, and
`pm_prefix` on top of it for a private reply — so the defaults cost 10 and 19 characters of the
first line respectively. That is deliberate: adding them *after* wrapping would push the first line
past what the engine will display, and several of these engines drop the overflow rather than
wrapping it (a Call of Duty console shows 65 characters and no more). If you are running a title
with a short chat limit and want the room back, shorten or empty them:

```yaml
bot:
  prefix: ""        # the bot says exactly what it was given
  pm_prefix: ""
```

On Insurgency with SourceMod's **"B3 Say"** plugin installed the prefix is dropped automatically —
that plugin draws the bot's messages distinctly itself, so a second marker is only clutter. The bot
detects it at startup with `sm plugins list`; nothing needs configuring.

## Scheduling

Plugins run timed work through the core scheduler — the classic bot's `PluginCronTab`:

```python
class MyPlugin(Plugin):
    def on_startup(self) -> None:
        self.schedule(self.announce, second=0, minute="*/15")  # every 15 minutes
        self.schedule(self.nightly, second=0, minute=0, hour=4)  # 04:00 in bot.time_zone
```

Fields are `second` (default `0`), `minute`, `hour`, `day`, `month`, `dow`, and accept the classic
syntax: `*`, `N`, `*/N`, `a-b`, `a-b/N` and comma-separated combinations. **Day-of-week is 0 =
Monday**, as in the legacy code. Pass `one_shot=True` to fire once.

Fields are validated when you register, so a bad schedule fails loudly at startup instead of silently
never firing. A schedule belongs to its plugin: it does not fire while that plugin is disabled, and it
is removed when the plugin unloads. A handler that raises is logged and the other schedules continue.
Handlers may be sync or async.
