# Big Brother Bot (B3) 2.0

A modern, typed, `asyncio`-based rewrite of [Big Brother Bot](https://github.com/BigBrotherBot/big-brother-bot),
the game-server admin bot: it watches a game server, keeps a database of who played and what they did,
and gives your admins commands to act on it.

**Jump to:** [what 2.0 is](#what-20-is) · [try it](#try-it) ·
[running one bot per game server](#running-b3-one-bot-per-game-server) ·
[in-game commands](#in-game-commands) · [configuration](#configuration) ·
[supported games](#supported-games)

## What 2.0 is

The classic bot is a ~87,000-line Python **2.7** program begun in 2005. It still works, and it still
carries everything it accumulated on the way: two config formats, five threads, event ids assigned at
runtime and injected into module globals, a hand-rolled SQL builder, and calls out to
`bigbrotherbot.net` services that no longer exist.

2.0 is a **ground-up rebuild of everything underneath, with the domain logic transplanted rather than
reinvented**. The log grammars, the RCON protocols, the client/penalty/group model and every hard-won
quirk in them are ported deliberately, because they encode fifteen years of bug reports from real
servers. What they sit on is all new:

| | classic B3 | 2.0 |
|---|---|---|
| Python | 2.7 (end of life 2020) | 3.11+, fully typed, `mypy --strict` clean |
| Concurrency | 5 threads plus a timer per plugin | one `asyncio` loop, no threads |
| Config | XML *and* INI, plus a proxy object | one validated YAML file |
| Commands | registered by the `admin` plugin, so every plugin depended on it | a core service with a typed registry |
| Storage | a string-concatenating query builder | SQLAlchemy models, Alembic migrations, a legacy-DB importer |
| Events | ids assigned at runtime into module globals | a static `EventType` enum |
| Plugins | copy files into a folder | `b3 plugin install owner/repo`, pinned to a semver tag |
| Dead services | master server, version check, publist, RSS feed | deleted — [see below](#what-was-deleted-and-what-was-rebuilt) |
| First-run failures | start it and read a traceback | `b3 doctor`, which tells "wrong password" from "wrong port" |

**What that buys an operator**: one config file to edit, a bot that will not silently half-work,
migrations instead of manual `ALTER TABLE`, plugins that install by name and pin to a version, and
straight answers when something is misconfigured.

**Where it stands today.** All 59 classic admin commands at the classic levels, 30 of the classic bot's
36 game titles across six engine families, and every core service the old bot had — plus remote log
tailing, per-server deployment and pre-flight checks it never had. 1,018 tests, `mypy --strict` clean.

## What was deleted, and what was rebuilt

A good deal of the classic bot was machinery for services that no longer exist. Removing it was the
original developer's own instruction, and it is most of the difference between 87,000 lines and 12,000.

**Deleted outright — nothing to port.** Every one of these called a `bigbrotherbot.net` endpoint that
has been unreachable for years:

| Legacy | What it did |
|---|---|
| `b3/update.py` | Polled `master.bigbrotherbot.net/version.json` on startup to nag about updates |
| `b3/pkg_handler.py` | The self-updater that went with it |
| `publist` plugin | Sent a heartbeat to `bigbrotherbot.net/master/serverping.php` to list your server publicly |
| `adv` plugin's news feed | Rotated in headlines from `forum.bigbrotherbot.net`'s RSS, via a `feedparser` dependency. The forum was the plugin-sharing hub; when it closed, the feed and the distribution model went with it |
| the config generator | Setup pointed you at a web page on the same dead domain to write your XML |

`b3 plugin install owner/repo` replaces that last one properly: plugins come from git, pinned to a
semver tag, instead of from a forum thread.

**Rebuilt rather than ported.** These were not dead, but they were the parts fifteen years of patches
had worn through:

| Legacy | Why it went | Now |
|---|---|---|
| `querybuilder.py` — SQL assembled by string concatenation | A live injection surface, and the plugin API handed it to third-party code (`storage.query()`) | SQLAlchemy models, Alembic migrations, a typed storage contract, and `Plugin.storage_engine()` for plugins that own tables |
| Event ids assigned at runtime and injected into module globals | `EVT_CLIENT_SAY` only existed once something had created it, so a typo was a runtime surprise | a static `EventType` enum — importable, type-checked, greppable |
| Command registration inside the `admin` plugin | Every other plugin therefore depended on `admin` | a core command service with a typed registry and permission bands |
| XML config, INI plugin configs, and a `MainConfig` proxy reconciling them | Three formats to get wrong, and a web page to generate one of them | one YAML file, validated by Pydantic, with `b3 doctor` to check it |
| **87 thread and timer constructions** across the tree (35 in the core and parsers, 52 more spread over 25 plugins) | Each plugin scheduling its own `threading.Timer` was the source of the bot's least reproducible bugs | one `asyncio` loop, a cron service plugins register with, and a scheduler that is testable against a fake clock |
| `getLineParts` + `On<Capwords>` reflection dispatch | A log line found its handler by string-munging a method name; a renamed method silently stopped being called | a declarative `@handles(regex)` registry, matched in definition order |
| `q3a/rcon.py` | Its Python 3 port was broken — `str(sock.recv())` corrupted every reply — in the most load-bearing module in the project | dialect / transport / orchestrator split into three testable pieces, with the retry rules preserved |
| Four threads, a lock and three queues in the BattlEye client | Its own comments record the trouble that caused | a single-threaded client that does its work when the caller asks |

**Preserved deliberately.** The opposite of the above: the log grammars, the RCON verbs per title, the
group bitmask model, the penalty semantics, the warning ladder's arithmetic, and every quirk with a
story behind it — CoD4 omitting the attacker's team on kill lines, `briefcase_bomb_mp` not counting as a
team kill, Urban Terror numbering weapons differently on hit lines than on kill lines. Those are ported
faithfully, with a regression test each, because they *are* the fifteen years of bug reports.

**In active development**, in this order: the remaining game titles, then more of the classic plugins
ported, then a web API and a dashboard over several servers at once.

## Try it

```bash
python -m pip install -e ".[dev]"
pytest                                            # 1,018 tests
cd examples && python -m b3.cli -c b3.yaml replay games_mp.log   # offline replay demo
```

The replay demo boots the bot against a recorded CoD4 log: a player claims superadmin with
`!iamgod`, then bans another player — issuing RCON and recording the penalty in SQLite.

---

# Reference

## CLI

Every command takes `-c/--config <path>` (default `b3.yaml`) and `-v/--verbose` (debug logging).
Installed as `b3`; equivalently `python -m b3.cli`.

### Running

| Command | What it does |
|---|---|
| `b3 init <dir>` | Create a bot instance for one game server — config, plugin config, `plugins/`, optional systemd unit. See [Running B3](#running-b3-one-bot-per-game-server) |
| `b3 -c b3.yaml doctor` | Check this install before starting it: RCON, game log, database, plugins |
| `b3 -c b3.yaml run` | Connect to the server, tail the game log (locally or [over the network](#tailing-a-hosted-servers-log)), and run until stopped |
| `b3 -c b3.yaml replay <logfile>` | Replay a recorded log offline — no server, no RCON. The test/demo harness |
| `b3 games` | Every valid `server.game`, grouped by engine, marking which need no game log. Needs no config |
| `b3 -c b3.yaml plugins` | Every plugin available here — bundled, installed for this server, installed in the shared pool — and which ones this config runs |

`init` flags: `--name`, `--game`, `--host`, `--port`, `--rcon-password`, `--game-log`,
`--command-file` (Altitude only), `--database`, `--shared-plugins-dir`, `--service` (write a systemd
unit), `--service-user`, `--force`.

`--game` only accepts a title the bot actually reads, and so does the config: an unrecognised
`server.game` stops the bot at startup with the near match named (`unknown game 'bf3_typo' — did you
mean 'bf3'?`) rather than falling back to a parser that would silently match nothing.

### Database

Schema is managed by Alembic; a fresh database is created and stamped automatically on first run.

| Command | What it does |
|---|---|
| `b3 db current` | Show the applied migration revision |
| `b3 db head` | Show the latest available revision |
| `b3 db upgrade [--revision REV]` | Apply pending migrations (default: `head`) |
| `b3 db stamp [--revision REV]` | Mark the database as being at a revision without running it |
| `b3 import-db <sqlalchemy-url>` | Import a legacy (Python-2 era) B3 database |

Legacy import preserves client ids, group bitmasks, epoch timestamps, and penalty semantics;
dangling `admin_id` references are nulled and orphaned penalties/aliases are skipped (the old
MyISAM schema had no foreign keys), with a summary report of exactly what was imported or skipped.

### Plugins

| Command | What it does |
|---|---|
| `b3 plugin install <spec>` | Clone, validate, seed config, record, and activate a plugin |
| `b3 plugin list` | Show installed plugins in both pools: name, version, ref, commit, origin |
| `b3 plugin enable <name>` | Run an already-installed plugin on this server — no download |
| `b3 plugin disable <name>` | Stop running it here; the files stay for other servers |
| `b3 plugin update <name> [--ref REF]` | Move to a newer ref (default: highest semver tag) |
| `b3 plugin remove <name> [--keep-files]` | Drop from disk, lockfile and config |

`install` flags: `--ref REF` (tag/branch, overrides any `@ref` in the spec), `--disabled` (activate
but do not run), `--no-enable` (install files only, leave the config alone), `--force` (reinstall
over an existing copy), `-y/--yes` (skip the confirmation prompt), `--shared` (install into
`bot.shared_plugins_dir` instead of this server's own directory).

Accepted specs — all optionally pinned with `@<tag-or-branch>`:

```bash
b3 plugin install owner/repo                      # GitHub shorthand, pinned to the highest tag
b3 plugin install owner/repo@v1.2.0               # an exact tag
b3 plugin install github.com/owner/repo@main      # a branch (must be named explicitly)
b3 plugin install https://github.com/owner/repo.git
b3 plugin install git@github.com:owner/repo.git
b3 plugin install /path/to/local/repo             # local clone
```

**Pinned by default:** a bare URL resolves to the repo's highest semver *tag*. Tracking a branch head
requires naming the branch, so "install" always means a reproducible version. The resolved commit SHA
is recorded in `<plugins_dir>/installed.yaml`.

**Trust:** a plugin runs in-process with full access to your database and game server — there is no
sandbox, exactly as in the classic bot. `install` prints what it will fetch and asks before doing it,
and *never* executes anything from the repo (the manifest is data; there is no post-install hook by
design). Trust is per-repository and yours to grant.

**Install takes effect at next start.** These are offline file operations; a bot already running in
another process will not pick them up.

**One bot per game server**, as in the classic bot: the code is installed once and each game server gets
an *instance directory* holding its config, its plugin configs, its plugins and its database. `b3 init`
creates one.

See [Running B3](#running-b3-one-bot-per-game-server) for the whole layout.

### Writing an installable plugin

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

MESSAGES = {"chatlog_empty": "no chat recorded yet"}   # operators can reword these

class ChatLoggerPlugin(Plugin):
    requires_plugins = ("admin",)

    def on_load_config(self):
        self.log_chat = (self.config or {}).get("general", {}).get("log_chat", True)

    def on_startup(self):
        self.register_messages(MESSAGES)                    # your text, still customisable
        Base.metadata.create_all(self.storage_engine())     # your own tables
        self.subscribe(EventType.CLIENT_SAY, self.on_chat)  # gated on the plugin being enabled
        self.schedule(self.prune, hour=4)                   # cron work, torn down on unload

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

## Bundled plugins

`admin` is always there. These are ported from the classic tree and switched on in the `plugins:`
list, each with an optional config of its own (see `examples/`):

| Plugin | What it does | Adds |
|---|---|---|
| `admin` | the 59 commands, groups, warnings | — |
| `censor` | bad language in chat, bad player names | — |
| `spamcontrol` | scores chat and warns whoever floods it | `!spamins [player]` |
| `pingwatch` | removes players whose connection is spoiling the game | `!ci <player>` |

Anything else is a git install — see [Plugins](#plugins) and
[Writing an installable plugin](#writing-an-installable-plugin).

## Running B3: one bot per game server

**The rule: one B3 process per game server.** A process tails one log and holds one RCON connection, so
it speaks to exactly one game server — the same model the classic bot used. Two game servers means two
processes.

What is *not* duplicated is the code. B3 is installed **once** per machine, and each game server gets a
small **instance directory** holding only its own settings:

```
/opt/b3/venv/               the code — installed once, shared by every bot
/opt/b3/plugins/            optional: a plugin pool every bot can draw from
/srv/cod4_1/b3/             instance 1  ── b3.yaml, its plugins, its database
/srv/cod4_2/b3/             instance 2  ── the same again, independent
```

Which instance a process *is* comes from `-c`: `b3 -c /srv/cod4_1/b3/b3.yaml run` **is** "the bot for
game server 1". There is no global config and no daemon managing the others — each is an ordinary
process you can start, stop and read the logs of on its own.

### One game server

```bash
python3 -m venv /opt/b3/venv
/opt/b3/venv/bin/pip install b3ng
ln -s /opt/b3/venv/bin/b3 /usr/local/bin/b3

b3 init /srv/cod4_1/b3 \
    --name cod4_1 \
    --game cod4x \
    --host 127.0.0.1 --port 28960 --rcon-password secret1 \
    --game-log /srv/cod4_1/main/games_mp.log \
    --service

b3 -c /srv/cod4_1/b3/b3.yaml doctor          # check before starting — see below
sudo cp /srv/cod4_1/b3/b3-cod4_1.service /etc/systemd/system/
sudo systemctl enable --now b3-cod4_1
```

`init` writes a commented `b3.yaml` with your answers already in it, a `plugin_admin.yaml`, an empty
`plugins/`, and — with `--service` — a systemd unit that knows exit code 221 from `!restart` means
"start me again" rather than "this crashed".

### Several game servers on one machine

Run `init` once per server. The only things that *must* differ are the **port**, the **game log** and the
**instance directory**; give each a `--name` so unit files and log lines are distinguishable.

```bash
b3 init /srv/cod4_1/b3 --name cod4_1 --game cod4x --port 28960 --rcon-password secret1 \
    --game-log /srv/cod4_1/main/games_mp.log --shared-plugins-dir /opt/b3/plugins --service
b3 init /srv/cod4_2/b3 --name cod4_2 --game cod4x --port 28970 --rcon-password secret2 \
    --game-log /srv/cod4_2/main/games_mp.log --shared-plugins-dir /opt/b3/plugins --service
b3 init /srv/urt_1/b3  --name urt_1  --game iourt43 --port 27960 --rcon-password secret3 \
    --game-log /srv/urt_1/q3ut4/games.log --shared-plugins-dir /opt/b3/plugins --service

for cfg in /srv/*/b3/b3.yaml; do b3 -c "$cfg" doctor || echo "FIX $cfg"; done
sudo cp /srv/*/b3/b3-*.service /etc/systemd/system/
sudo systemctl enable --now b3-cod4_1 b3-cod4_2 b3-urt_1
```

They need not be the same game — the instance decides — so one machine can run a CoD4X bot, an Urban
Terror bot and a BF3 bot side by side.

Day to day, each is addressed by its own config or unit:

```bash
b3 -c /srv/cod4_2/b3/b3.yaml doctor              # is server 2 healthy?
b3 -c /srv/cod4_2/b3/b3.yaml plugin list         # what is server 2 running?
sudo systemctl restart b3-cod4_2                 # restart just that one
sudo journalctl -u b3-cod4_2 -f                  # follow just that one
```

### Adding a server later

Nothing to reconfigure: `b3 init` the new directory, `doctor` it, install its unit. The existing bots are
untouched — they do not know about each other.

### What lives where, and why

```
/opt/b3/
    venv/                    the code. Upgrade once: /opt/b3/venv/bin/pip install -U b3ng
    plugins/                 optional shared plugin pool (bot.shared_plugins_dir)
/srv/cod4_1/b3/
    b3.yaml                  this server's settings — the RCON password lives here
    plugin_admin.yaml        this server's admin policy (ban durations, warn ladder, rules)
    plugins/                 plugins installed for THIS server only (bot.plugins_dir)
    b3.sqlite                this server's database, unless it points at a shared one
```

Two details that exist because of real breakage:

- **`plugins_dir` defaults to `@conf/plugins`** — next to the config, so it is per *instance*. An earlier
  default put it under `~/.b3/plugins`, machine-wide, which meant two servers could not run different
  versions of a plugin and `plugin remove` on one deleted the other's files.
- **A relative `sqlite:///b3.sqlite` is anchored to the config's own directory.** Otherwise starting a
  bot from a different working directory silently creates a second, empty database — and the symptom is
  "all my admins lost their levels", which points nowhere near the cause.

### Check before starting — the step that saves the evening

```
$ b3 -c /srv/cod4_1/b3/b3.yaml doctor
[  ok  ] game          cod4x, server 127.0.0.1:28960
[  ok  ] time zone     UTC
[  ok  ] database      sqlite:////srv/cod4_1/b3/b3.sqlite: 0 client(s), writable
[ FAIL ] game log      /srv/cod4_1/main/games_mp.log does not exist
                       -> point server.game_log at the game's games_mp.log (check g_log / fs_game on the server)
[ FAIL ] rcon          the server rejected the password
                       -> server.rcon_password must match rcon_password on the game server
[  ok  ] plugin admin  imports cleanly

2 problem(s) to fix before this bot will work properly.
```

"The server rejected the password" and "nothing answered on that port" are different problems with
different fixes, and the classic bot showed the same silence for both. `doctor` exits non-zero if
anything failed, so a deploy script can gate on it — which is what the `for` loop above does.

### Sharing plugins between servers

Install a plugin **once** into the pool, then switch it on per server — no second download, and each
server still decides for itself:

```bash
b3 -c /srv/cod4_1/b3/b3.yaml plugin install owner/repo --shared   # into /opt/b3/plugins
b3 -c /srv/cod4_2/b3/b3.yaml plugin enable chatlogger             # server 2 wants it too
b3 -c /srv/cod4_3/b3/b3.yaml plugin install owner/repo@v1.0.0     # server 3 pins its own copy
b3 -c /srv/cod4_2/b3/b3.yaml plugin disable chatlogger            # off here; files stay for others
```

`b3 plugin list` shows both pools. A copy installed for one server **overrides** the shared one, so a
server can pin an old version — or test a new one — without disturbing the others. This is the classic
bot's `external_dir` idea (files shared, each server's config deciding what runs), with the sharing made
explicit rather than implied by a path.

### Sharing players and bans between servers

Point several instances at one MySQL/Postgres `database:` URL and they share one identity namespace: a
ban on any server applies everywhere, your admins are admins everywhere, and alias/IP history is merged
— because `guid` is the natural key. Keep the URLs separate to keep the servers independent. (Shared
means MySQL or Postgres — `pip install b3ng[mysql]` — not SQLite on a file share.)

When you do share one, set **`bot.server_id`** in each instance so the table records *which* server
issued each penalty:

```yaml
bot:
  database: "mysql+pymysql://b3:pw@10.0.0.5/b3"
  server_id: "cod4_1"        # written on every penalty this bot issues
```

It is attribution, not scoping: enforcement still reads every server's penalties, because a shared ban
list that only applied where it was typed would defeat the point of sharing one. Leave it unset on a
single-server install. The classic bot had no equivalent, which is why its shared databases could never
say where a ban came from.

### The bot need not run on the game server

`server.game_log` accepts `ftp://`, `ftps://`, `sftp://` and `http(s)://` URLs, so one VPS can run every
bot while the game servers live elsewhere — the log is tailed over the network and RCON is UDP anyway.
See [Tailing a hosted server's log](#tailing-a-hosted-servers-log).

On Arma and Battlefield titles there is no log at all — the RCON connection carries the events — so those
bots run anywhere that can reach the RCON port, and `server.game_log` is ignored entirely.

### Without systemd

The unit file is a convenience, not a requirement: `b3 -c <config> run` is an ordinary foreground
process. Anything that restarts a process will do, provided it treats **exit code 221 as "start me
again"** (that is `!restart`) and **0 as "stay stopped"** (that is `!die`). On Windows, use one scheduled
task or service per instance directory, each with its own `-c`.

## In-game commands

Chat prefixes: `!cmd` replies privately, `@cmd` and `&cmd` broadcast the reply, `/cmd` is silent.
Broadcasting needs level 9 (`bot.loud_level`) and the silent prefix level 80 (`bot.silent_level`) —
a fresh player should not be able to make the bot shout at the whole server.

| Command | Alias | Level | What it does |
|---|---|---|---|
| `!help` | `!h` | 0 | List the commands you can use |
| `!iamgod` |  | 0 | Claim superadmin (only works while the server has no superadmin) |
| `!register` |  | 0 | Register yourself as a basic user |
| `!rules` | `!r` | 0 | Say the server rules |
| `!nextmap` |  | 1 | Show the next map in the rotation |
| `!regtest` |  | 1 | Show your own group and level |
| `!regulars` | `!regs` | 1 | List the regular players currently connected |
| `!time` |  | 1 | The server's current time |
| `!maps` |  | 2 | List the server's map rotation |
| `!seen <player>` |  | 2 | When a player was last seen |
| `!admins` |  | 20 | List the admins currently connected |
| `!aliases <player>` | `!alias` | 20 | List the other names a player has used |
| `!b3` |  | 20 | What this bot is |
| `!find <player>` |  | 20 | Find a connected player |
| `!leveltest [player]` | `!lt` | 20 | Show a player's group and level |
| `!list` |  | 20 | List the connected players and their slot ids |
| `!longlist` |  | 20 | List the connected players with id, level and ping |
| `!poke <player>` |  | 20 | Nudge a player who is not paying attention |
| `!say <message>` |  | 20 | Say something to everyone |
| `!spam <keyword>` | `!s` | 20 | Broadcast one of the configured messages |
| `!spams` |  | 20 | List the configured spam messages |
| `!status` |  | 20 | Report the bot's health and what the server is running |
| `!warn <player> [reason]` | `!w` | 20 | Issue a warning (a keyword from warn_reasons also sets its life) |
| `!warninfo <player>` | `!wi` | 20 | How many warnings a player is carrying |
| `!warnremove <player>` | `!wr` | 20 | Lift a player's most recent warning |
| `!warns <player>` |  | 20 | List a player's active warnings |
| `!warntest <reason>` | `!wt` | 20 | Show what a warning reason expands to |
| `!admintest` |  | 40 | Show your own group and level |
| `!baninfo <player>` | `!bi` | 40 | Show the ban currently in force on a player |
| `!kick <player> [reason]` | `!k` | 40 | Remove a player from the server |
| `!lastbans` | `!lbans` | 40 | List the bans currently in force |
| `!notice <player> <note>` |  | 40 | Record a note about a player |
| `!scream <message>` |  | 40 | Announce something in the engine's largest text |
| `!tempban <player> <duration> [reason]` | `!tb` | 40 | Ban for a limited time (e.g. 30m, 2h, 1d) |
| `!ban <player> [reason]` | `!b` | 60 | Ban a player for `ban_duration` (14 days by default) |
| `!spank <player> [reason]` | `!sp` | 60 | Kick a player, loudly |
| `!unban <player> [reason]` |  | 60 | Lift every active ban on a player (works offline, by @id) |
| `!banall <pattern> [reason]` | `!ball` | 80 | Ban every player whose name matches |
| `!clear [player]` | `!kiss` | 80 | Clear a player's warnings, or everyone's |
| `!clientinfo <player>` |  | 80 | Show a player's stored identity, level and history |
| `!kickall <pattern> [reason]` | `!kall` | 80 | Kick every player whose name matches |
| `!lookup <player>` | `!l` | 80 | Find a player in the database, connected or not |
| `!makereg <player>` | `!mr` | 80 | Make a player a regular |
| `!map <name>` |  | 80 | Change to another map |
| `!maprotate` |  | 80 | Advance to the next map in the rotation |
| `!pause <duration>` |  | 80 | Stop acting on the game for a while (0 to resume) |
| `!permban <player> [reason]` | `!pb` | 80 | Ban a player permanently, whatever ban_duration says |
| `!putgroup <player> <group>` |  | 80 | Put a player in a group (replaces their current one) |
| `!rebuild` |  | 80 | Re-read the player list from the server |
| `!spankall <pattern> [reason]` | `!sall` | 80 | Spank every player whose name matches |
| `!ungroup <player> <group>` |  | 80 | Remove a player from a group |
| `!unreg <player>` | `!ur` | 80 | Take a player out of the regulars, back to plain user |
| `!warnclear <player>` | `!wc` | 80 | Clear a player's active warnings |
| `!die` |  | 100 | Shut the bot down |
| `!mask <group> [player]` |  | 100 | Appear to be in a lower group than you are |
| `!reconfig` |  | 100 | Re-read the configuration file |
| `!restart` |  | 100 | Stop with a restart code, for whatever supervises the bot |
| `!plugin` |  | 100 | `list`, `info <name>`, `enable <name>`, `disable <name>` — turn a plugin on or off without restarting |
| `!runas <player> <command>` | `!su` | 100 | Run a command as someone else |
| `!unmask [player]` |  | 100 | Stop hiding a level |

That is **all 59 of the classic bot's admin commands**, at its `plugin_admin.ini` default levels
(guest 0, user 1, reg 2, mod 20, admin 40, fulladmin 60, senioradmin 80, superadmin 100).

`<player>` resolves as a slot id (`3`), a case-insensitive partial name (`bo` → Bob), or a database
id (`@42`). Commands that act on people who have already left — `!unban`, `!baninfo`, `!aliases`,
`!warns`, `!warnclear`, `!clientinfo`, `!putgroup`, `!ungroup`, `!makereg`, `!unreg`, `!leveltest`,
`!seen`, `!lookup` — also search stored names **and past aliases**, so someone who renamed themselves
is still findable. When a name matches several stored players the bot lists the candidates with their
`@id` instead of guessing.

A few commands deliberately behave differently from the classic bot:

- **`!restart` exits with code 221** rather than re-execing itself. Under systemd, Docker or a
  supervisor script the exit code *is* the restart signal.
- **`!pause <duration>` is a deadline, not a thread.** The bot keeps tailing but ignores what
  happens, and resumes by itself when the time is up — so note that you cannot `!pause 0` your way
  out early from in-game, because chat is ignored too.
- **`!status`** reports bot health *and* the current map and player count; the classic version only
  said whether the database was up.
- **`!scream`** uses the engine's big-text verb once instead of repeating a `say` five times, and
  **`!b3`** reports version and plugin counts without the classic version's joke responses.
The policy defaults are the classic bot's, verbatim, and all of them are config: **`!ban` is a
14-day tempban** and `!permban` is the permanent one (`ban_duration`); **a reason is compulsory**
for everyone below superadmin (`noreason_level`); and `!tempban` is capped at 3h below senioradmin
(`long_tempban_level` / `long_tempban_max_duration`). Set `ban_duration: 0` and `noreason_level: 0`
if you prefer `!ban` to mean forever and reasons to be optional.

### Groups, reach and masking

`<group>` is a keyword from the `groups` table: `superadmin`, `senioradmin`, `fulladmin`, `admin`,
`mod`, `reg`, `user`, `guest` — or whatever you renamed them to, since the commands read the table
rather than assuming the seeded defaults. `!putgroup` **replaces** a player's group, so it both
promotes and demotes; `!ungroup` removes one membership and leaves the others.

Two rules bound what an admin can do:

- **Group reach.** You cannot hand out a group at or above your own level. Only a superadmin can
  create another admin of equal standing.
- **Equals and betters.** You cannot kick, ban, warn, promote or demote yourself, or anyone whose
  level is greater than or equal to yours — superadmins included. The classic bot enforced this for
  penalties but not for the group commands, which left a senioradmin able to demote a superadmin.

`!mask` hides rank rather than removing it: a masked superadmin still has every power, but
`!leveltest` and `!admins` show (and filter by) the masked level, and a refusal aimed at them reads
as "masked higher-level player" instead of announcing who they really are. `!unmask` clears it.
The mask is stored, so it survives a reconnect.

### Rules, spam messages and reason keywords

The admin plugin takes an optional config file of its own (`examples/plugin_admin.yaml`), pointed at
from the main config with `config: "@conf/plugin_admin.yaml"`. Everything in it is optional.

```yaml
settings:
  ban_duration: 0            # 0 = !ban is permanent; "14d" restores the classic behaviour
spamages:                    # canned messages for !spam <keyword>
  rule1: "^3Rule #1: ^7no racism of any kind"   # rule1..rule20 are also what !rules says, in order
  vent: "^3Voice chat: ^7vent.example.com"
warn_reasons:                # shortcuts usable as the reason on !warn, !kick, !ban, !spank, ...
  lang: "3h, ^7watch your language"   # "<duration>, <text>" — the duration is how long the warning lives
  cuss: "/lang"                       # point at another entry
  racism: "/spam#rule1"               # or at a spam message
  camp: "1h, ^7stop camping"
```

Typing `!warn Bob lang` then records "watch your language" as a warning that expires in three hours,
and `!warntest lang` shows you exactly that without warning anybody. An unknown keyword is simply
used as the reason text, so free-form reasons keep working.

### What warnings add up to

Warnings are not just a record — they escalate, on a ladder you configure in the same file under
`warn:`. With the defaults:

1. Every warning is announced with the player's running total: `WARNING [2]: Bob, watch your language`.
2. At **3** warnings the bot announces that Bob is about to be banned, and gives an admin **25
   seconds** to `!clear` him. Clearing the warnings inside that window cancels it — nothing else to do.
3. At **5** he is tempbanned at once, with no alert.
4. How long for? The **sum of his warnings' own durations, divided by 30** and capped at a day — so
   somebody warned for trivia is out for minutes, and somebody warned for serious things for hours.
   Past **6** warnings that scaling stops and the flat `tempban_duration` applies.
5. `delay: 15` stops two admins warning the same player three times in a second and tripping the
   ladder by accident.

This counts warnings from **any** source: a warning issued by another plugin escalates exactly like
one from `!warn`, because the bot hangs escalation off the warning event rather than off the command.
The grace period is checked by the bot's scheduler, so — unlike the classic bot — there is no timer
thread waiting to fire into a shut-down bot.

### Knowing who is really on the server

A game log tells the bot what *happened*; it cannot tell it what *is*. A join line carries no IP
address, and a bot started mid-match has seen nobody join at all. Both are solved by asking the
server directly, over RCON:

- **Authentication finishes asynchronously.** When a player joins without an IP, the bot polls the
  server's `status` table in the background until that slot appears, then fills in the address and
  records it in the player's IP history. Nothing waits on it; if the player leaves first, the poll
  is cancelled.
- **The player list is reconciled every 5 minutes.** Players the bot never saw join are adopted
  (and authenticated, so their bans and level apply immediately), players the server no longer
  lists are dropped, and a slot taken over by someone else is replaced rather than reused. This is
  what makes it safe to start or restart the bot in the middle of a match.
- **Match state is tracked** from `InitGame`: current map, gametype, round count and how long the
  map has been running, plus the cvars the bot has read. `!status` reports it, and plugins can read
  it from `console.game` without a round trip.

## Configuration

One typed YAML file, validated at load — a bad value fails immediately with a clear message.
Path values accept tokens: `@b3` (the installed package dir), `@conf` (the main config's directory),
`@home` (`~/.b3`), and `~`.

```yaml
bot:
  name: b3                            # default: b3
  prefix: "^2(b3)^7:"                 # in-game message prefix (game colour codes allowed)
  time_zone: UTC                      # IANA name; schedules are evaluated in this zone
  log_level: INFO
  line_length: 90                     # game chat limit; longer replies wrap across lines
  line_color_prefix: ""                # prepended to each continuation line, e.g. "^3"
  database: "sqlite:///b3.sqlite"     # any SQLAlchemy URL (sqlite / mysql+pymysql / postgresql+psycopg)
  server_id: ""                       # names this server on the penalties it issues; only
                                      # needed when several bots share one database
  plugins_dir: "@home/plugins"        # where `b3 plugin install` puts things

server:
  game: cod4x                         # cod4x (the CoD4X 1.8 mod) or cod4 (stock 1.7)
  rcon_password: "changeme"
  host: 127.0.0.1
  port: 28960
  game_log: "games_mp.log"            # local path, or an ftp/sftp/http URL (see below)
  encoding: latin-1                   # CoD engines are latin-1
  rcon_timeout: 0.8
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

### Supported games

Six engine families, thirty titles — `b3 games` prints the list on any install. Set `server.game` to
one of them:

| Family | `server.game` |
|---|---|
| **Call of Duty** | `cod2` `cod4` `cod4x` `cod4gr` `cod5` `cod6` `cod7` `cod8` |
| **BattlEye** | `arma2` `arma3` |
| **Frostbite** | `bfbc2` `moh` `bf3` `bf4` `bfh` `mohw` |
| **Quake 3** | `q3` `oa081` `smg` `smg11` `wop` `wop15` |
| **Quake 3 — Urban Terror** | `iourt41` `iourt42` `iourt43` |
| **Quake 3 — Enemy Territory** | `et` `etpro` |
| **Quake 3 — Soldier of Fortune 2** | `sof2` `sof2pm` |
| **Altitude** | `altitude` |

A family is one parser; the titles in it are data — GUID length, ban verbs, the shape of the status
table. Adding a title to a family is a few lines; adding a family is a parser.

The two families differ in one structural way worth knowing. A CoD log repeats a player's identity
on every line; a Quake3 log states it **once**, in an infostring when the player connects, and
every later line is a bare slot number. Quake3 chat lines carry a *name* rather than a slot, so a
player the bot never saw connect produces no event rather than a nameless one.

The three Urban Terror / ET / SoF2 rows are the shared Quake3 grammar **plus** that title's own
lines, read by a subclass — a title never loses a line by gaining a family of its own:

- **Urban Terror** — `Hit:` (non-fatal hits with a hit location, which is what makes team-damage
  policing possible), `Radio:`, flags, bomb mode and the voting lines. A hit line carries no damage
  figure, so the damage comes from a weapon×hit-location table, and hit lines number weapons on a
  *different* scale from kill lines. Not included: UrT's `auth` account service, and the freeze-tag
  and jump-run lines (they belong with the plugins that want them).
- **Enemy Territory** — `ConnectInfo:`, which is where an ET player's identity comes from: a plain ET
  server sets no `cl_guid` at all, so without this line nobody could hold a level or a ban. Also
  `Gib:` and ET's own slot-numbered chat lines, which beat matching a colour-coded name.
- **Soldier of Fortune 2** — its `Hit:` line, which states the damage outright.

### Battlefield / Medal of Honor (Frostbite)

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

### Arma 2 / Arma 3 (BattlEye)

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

### Altitude

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

### Which CoD4 are you running?

Nearly every live CoD4 server runs **CoD4X 1.8** rather than the stock 1.7 binary, and they differ in
ways that matter. Set `server.game` accordingly:

`server.game` accepts every Call of Duty title the classic bot supported: `cod2`, `cod4`, `cod4x`,
`cod4gr` (GameRanger), `cod5`, `cod6` (MW2), `cod7` (Black Ops), `cod8` (MW3). They share one parser
— the differences are data. All of them are asked for `g_logsync` on connect, without which the
engine buffers its log file and the bot reacts late or not at all.

Two caveats: **cod6/cod8 have no working ban verb**, so a ban there is a kick the bot re-applies on
every reconnect (as it was classically); and **cod7 logs** were served by Activision's long-dead
"Elite" service, so Black Ops needs a server that writes a log file the bot can reach. Its RCON
framing differs from the other titles and is handled.

| | `cod4` (stock) | `cod4x` (CoD4X 1.8) |
|---|---|---|
| Identity | 32-character GUID | **Steam64 id** — the bot sends `sv_usesteam64id 1` on connect, and keys players on the Steam id rather than the per-session GUID beside it |
| Ban | `banclient` | `permban <slot> <reason>` |
| Timed ban | none — the bot re-applies it on reconnect | **native** `tempban <slot> <mins>m <reason>`, capped by the engine at 30 days |
| Unban | by name, connected players only | `unban <guid>` — reaches someone who has left |
| Reasons | not shown to the player | passed to the engine, so the player sees why |

Getting this wrong is loud, not silent: pointing `cod4x` at a server that is not reporting Steam64
ids makes the bot log *"ignoring guid … Nobody will be authenticated while this is the case"* rather
than quietly failing to recognise anybody.

A tempban longer than CoD4X allows is still recorded in full — the engine gets its 30-day maximum,
and the bot re-applies the remainder when the player next connects.

### Tailing a hosted server's log

`server.game_log` is normally a path, which means the bot runs on the game server's own box. Point it
at a URL instead and it tails the log over the network — the standard way to run against a **hosted**
server you have no shell on (the classic bot's `ftpytail` / `sftpytail` / `httpytail` plugins).

| Scheme | Notes |
|---|---|
| *(a path)* | Local file. No polling delay, no extra dependencies. |
| `ftp://user:pass@host[:port]/path/games_mp.log` | Resumes with `REST`, binary mode, passive (game hosts are behind NAT). |
| `ftps://…` | Same, over implicit TLS, with the data connection encrypted too. |
| `sftp://user:pass@host[:port]/path/games_mp.log` | Needs `paramiko`: `pip install b3ng[sftp]`. With no password in the URL it uses your agent and `~/.ssh` keys. An unknown host key is accepted with a warning. |
| `http://…`, `https://…` | Polls `HEAD` for the size, then a ranged `GET`. Credentials in the URL become a basic-auth header. Servers that ignore `Range` still work. |

All of them tail by **byte offset**, so:

- Only new bytes are transferred; the offset survives a reconnect, so nothing is replayed or lost.
- A log that shrinks was rotated or truncated — reading restarts from the top.
- A gap bigger than `log_max_gap` (first poll, or a rotation you missed) is skipped rather than
  replayed, so the bot never acts on a flood of stale events. It resumes at the next full line.
- A dropped connection is retried with exponential back-off (5s, doubling, capped at 5 minutes) and
  logged; the bot keeps running. Passwords are redacted from every log line.
- A wrong host, path or password fails loudly at startup instead of quietly tailing nothing.

Percent-encode any `@` or `:` in a username or password (`p@ss` → `p%40ss`).

### How plugins load

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

### Messages

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

### Scheduling

Plugins run timed work through the core scheduler — the classic bot's `PluginCronTab`:

```python
class MyPlugin(Plugin):
    def on_startup(self) -> None:
        self.schedule(self.announce, second=0, minute="*/15")   # every 15 minutes
        self.schedule(self.nightly, second=0, minute=0, hour=4)  # 04:00 in bot.time_zone
```

Fields are `second` (default `0`), `minute`, `hour`, `day`, `month`, `dow`, and accept the classic
syntax: `*`, `N`, `*/N`, `a-b`, `a-b/N` and comma-separated combinations. **Day-of-week is 0 =
Monday**, as in the legacy code. Pass `one_shot=True` to fire once.

Fields are validated when you register, so a bad schedule fails loudly at startup instead of silently
never firing. A schedule belongs to its plugin: it does not fire while that plugin is disabled, and it
is removed when the plugin unloads. A handler that raises is logged and the other schedules continue.
Handlers may be sync or async.

## Domain rules

Preserved from the classic bot for data compatibility:

**Groups** — `id` is a power-of-two membership *bit*; a client's `group_bits` is the bitwise OR of
them. `level` (0–100) is the permission ordinal that commands compare against.

| bit | keyword | name | level |
|---|---|---|---|
| 128 | superadmin | Super Admin | 100 |
| 64 | senioradmin | Senior Admin | 80 |
| 32 | fulladmin | Full Admin | 60 |
| 16 | admin | Admin | 40 |
| 8 | mod | Moderator | 20 |
| 2 | reg | Regular | 2 |
| 1 | user | User | 1 |
| 0 | guest | Guest | 0 |

**Penalties** — active means `inactive=0 AND (time_expire=-1 OR time_expire>now)`; `-1` is permanent.
`duration` is in minutes. Lifting a penalty sets `inactive=1` — never a physical delete, so the audit
trail survives.

**Identity** — `guid` is the natural key; `id` is the surrogate. Alias and IP-alias tables keep
history with `num_used` counters. Timestamps are Unix epoch integers.

**Resilience** — the bot keeps running when the database is down.

## Requirements

- Python 3.11+
- `git` on `PATH` (only for `b3 plugin install/update`)
- No system time-zone database needed; `tzdata` is installed as a dependency

## Development

```bash
python -m pip install -e ".[dev]"
pytest                     # 278 tests
ruff check src tests
```

## License

GPL-2.0-or-later, inherited from the original B3 project.
