| [Overview](../README.md) | [CLI](cli.md) | [Plugins](plugins.md) | [Deployment](deployment.md) | [Commands](commands.md) | [Configuration](configuration.md) | [Games](games.md) | **Migrating** | [Development](development.md) |
|---|---|---|---|---|---|---|---|---|

# Migrating from B3 1.x

For anyone with a working classic B3 install. The short version: **your database comes across whole,
your config is converted for you with a report of what it could not convert, and your admins keep
their levels.**

Nothing here is destructive. The importer reads your old database and never writes to it, and 2.0
uses a different config file with a different name, so a half-finished migration leaves the old bot
exactly as it was. Run them side by side until you are satisfied.

---

## What carries over, and what does not

| | |
|---|---|
| ✅ **Clients, levels and groups** | Every player record, with their `group_bits` bitmask, mask level, greeting, login and password. Ids are preserved, so nothing that references them breaks |
| ✅ **Penalties** | Bans, tempbans, kicks and warnings, with their `type`, `inactive`, `time_expire` and `duration` carried verbatim — an active ban is still active after the move |
| ✅ **Aliases and IP history** | Both tables |
| ✅ **Custom groups** | Upserted rather than overwritten, so a renamed or re-levelled group survives |
| ✅ **Commands and their levels** | All 59, at the same levels the classic's `plugin_admin.ini` gave them |
| ⚠️ **The config** | Converted by `b3 import-config`, then read. XML + a dozen INI files became one YAML file, and the settings that changed meaning are **reported rather than written**, because a config that looks complete and is quietly wrong is worse than one you know is unfinished |
| ⚠️ **Plugin configs** | Same: one YAML per plugin, converted key by key against what that plugin still accepts. Your own writing — rules, warning reasons, custom commands — comes across whole |
| ❌ **Some plugins** | Seven became core services and four are gone. [Full table below](#what-happened-to-each-plugin) |
| ❌ **`b3/extplugins` drop-ins** | Third-party plugins are installed from git now (`b3 plugin install owner/repo`) and use a different plugin API |

---

## 1. Install, and check before you start

```bash
python -m pip install b3ng
b3 --version
```

## 2. Bring the config across

Do this **before** the database import, not after: `b3 import-db` reads `bot.database` from the
config to know where to import *to*, so there has to be a config first. Only `b3 init`,
`b3 import-config`, `b3 games`, `b3 completion` and `b3 version` run without one.

```bash
b3 import-config /path/to/classic/b3/conf --dry-run   # read the report, write nothing
b3 import-config /path/to/classic/b3/conf --out .     # then write it
```

It reads `b3.xml` and every `plugin_*.ini` (or `.xml`) beside it, writes `b3.yaml` and a
`plugin_*.yaml` for each plugin that is still a plugin, and **prints everything it would not convert,
with what to do about each one**. The classic ships 43 of those files and 2,991 lines of them, which
is why "just retype it" is an afternoon and a transposed digit nobody notices for a month.

**It converts what is provably safe and reports the rest rather than guessing at it.** That refusal
is the feature, not a limitation. A converter that copied every line across would produce a file that
looks complete and is quietly wrong wherever the same word now means something else — and a
plausible-looking config is worse than an obviously incomplete one, because you stop reading it. The
clearest case is `tk`'s `levels`: the classic listed which groups get penalised, and here every level
carries its own kill, damage and ban multipliers. Written through verbatim it is a key the plugin
ignores, so the setting you spent an evening tuning sits silently at its default. Reported, it is one
line of work.

So the report is the part to read. Three kinds of thing land in it:

* **A setting the plugin no longer has.** Checked against the plugin's own defaults rather than a
  list kept inside the converter, so this stays true as the plugins change. `[warn]`'s
  `alert_kick_num` is `alert_at` now, and three of its neighbours were renamed too.
* **A section that moved somewhere else.** `[commands]` set a plugin's command levels in the classic;
  here that is the `cmdmanager` plugin, deliberately, so that an override lives in one file instead
  of in whichever plugin happens to own the command. `[messages]` is the `messages:` block of
  `b3.yaml`.
* **A plugin that is not a plugin any more,** or one whose *config file* is gone though the plugin
  is not — `cmdmanager` keeps what it is told in its own tables now. The report names what took
  over. [Full table below](#what-happened-to-each-plugin).

Your own writing is not checked against anything. `[spamages]`, your warning reasons, your bad-word
and bad-name lists, your spree messages and your custom commands are your words rather than a
schema — an entry called `rule7` is neither known nor unknown, it is yours — so they come across
whole, including the ones whose section was renamed. Where the placeholders changed with the name
they are translated too: a spree message keeps its meaning, because `%player%` carried across
unchanged would not be a placeholder here, just the literal text the server prints.

Starting fresh instead of migrating? `b3 init --game cod4` asks you the questions.

What it does with `b3.xml`, for the settings most installs actually set:

| `b3.xml` | `b3.yaml` | Note |
|---|---|---|
| `b3/parser` | `server.game` | Now validated — an unknown value refuses to start instead of silently falling back to the CoD4 parser |
| `b3/database` | `bot.database` | Any SQLAlchemy URL, which needs the driver named: a `mysql://` URL is rewritten to `mysql+pymysql://` for you, and the report says so, because it changed your connection string |
| `b3/bot_name` | `bot.name` | |
| `b3/bot_prefix` | `bot.prefix` | |
| `b3/time_zone` | `bot.time_zone` | An IANA name (`Europe/Berlin`), not `CST`. An abbreviation is copied across **and reported**, never guessed at: `CST` is US Central and China Standard, six hours apart, and every timestamp the bot writes depends on which you meant |
| `b3/log_level` | `bot.log_level` | A word (`INFO`, `DEBUG`), not a number on the classic's own scale. Converted to the nearest, and reported so you can pick another |
| `b3/logfile` | — | Logs go to stdout; your service manager decides where. See [Deployment](deployment.md) |
| `server/rcon_password` | `server.rcon_password` | |
| `server/port` | `server.port` | |
| `server/rcon_ip`, `server/public_ip` | `server.host` | One setting, because they were the same address on every install that worked |
| `server/game_log` | `server.game_log` | A path **or** an `ftp:` / `ftps:` / `sftp:` / `http:` URL |
| `server/punkbuster` | `server.punkbuster` | Optional now: the profile knows which titles have it, and this only overrides |
| `server/delay`, `server/lines_per_second` | — | Gone. There is no read loop to pace: the bot is event-driven |
| `plugins/external_dir` | `bot.plugins_dir` | Or `bot.shared_plugins_dir` for a pool several bots share |
| `update/channel` | `bot.update_check`, `bot.update_remote` | Points at a repository you name; empty switches it off |
| `autodoc/*` | — | Gone. The command reference is generated from the code — you are reading it at [Commands](commands.md) |
| `messages/*` | `messages:` | Same keys, same `$variable` placeholders |

Each plugin's `plugin_x.ini` becomes a `plugin_x.yaml` written beside `b3.yaml`, and listed against
that plugin in `plugins:`. Read what came out against `examples/`, which has an annotated config for
every bundled plugin and is the fastest way to see what a section is called now — then run
`b3 doctor`, which checks the file, the database, the log and the RCON connection before you start.

## 3. Bring the database across

Create the 2.0 schema, then import into it:

```bash
b3 db upgrade                          # create/upgrade the 2.0 schema
b3 import-db sqlite:///old/b3.sqlite   # the OLD database's URL
```

The argument is your **old** database; the destination is the `bot.database` you just configured. The
old one is only ever read.

The importer reports what it did:

```
clients=1284 groups=8 penalties=3901 (skipped_orphan=12, admin_nulled=47)
aliases=5620 (skipped=3) ipaliases=2210 (skipped=0) data=4
```

Those `skipped` and `nulled` counts are worth reading rather than ignoring. **The classic's MyISAM
tables had no foreign keys**, so real databases accumulate references to rows that no longer exist —
a penalty issued by an admin whose record was later deleted, or the sentinel `admin_id` of `0`. The
2.0 schema does enforce foreign keys, so those rows cannot be loaded as they are. Rather than refuse
the whole import:

* a penalty whose **admin** no longer resolves keeps the penalty and nulls the admin (`admin_nulled`);
* a penalty or alias whose **subject** no longer exists is skipped, because there is no player for it
  to belong to (`skipped_orphan`).

Client ids are preserved throughout, which is what keeps every surviving reference valid.

!!! note "If your old database is MySQL"
    The classic wrote `mysql://user:pw@host/b3`. SQLAlchemy needs a driver in the URL:
    `mysql+pymysql://user:pw@host/b3`, with `pip install b3ng[mysql]`. The same applies to
    `bot.database` in the new config.

The import is idempotent — rows merge by primary key — so running it twice is safe, and you can
re-run it after the old bot has been live a little longer.

```bash
b3 doctor          # config, database, schema revision, game log and rcon, each checked
```

`b3 doctor` is the part of this worth knowing about before you need it. It tells "wrong password"
from "wrong port", which is the pair that used to cost an evening.

## 4. Check your game title still has that name

Three changes since the classic:

| Classic | Now | |
|---|---|---|
| `q3a` | `q3` | **`q3a` still works** — it is kept as an accepted alias, so an imported config does not break |
| `csgo` | `cs2` | CS:GO 1 was dropped and CS2 shipped in its place. There is no alias: they are different games with different log formats, and pretending otherwise would mis-parse everything |
| `ro2`, `chiv` | — | Dropped. Both used WebAdmin, which means scraping an admin *UI* that is rewritten whenever the game is patched — it cannot be kept correct the way a protocol can |

An unrecognised `server.game` now refuses to start and names the near match, rather than falling back
to the CoD4 parser without a word. `b3 games` prints all 38.

## 5. Plugins

List the ones you want in `plugins:`. Only `admin` is required.

### What happened to each plugin

| Classic plugin | Now |
|---|---|
| `admin` `censor` `spamcontrol` `pingwatch` `tk` `stats` `welcome` `afk` `banlist` `cmdmanager` `customcommands` `status` `login` `ipban` `nickreg` `makeroom` `callvote` `spree` `firstkill` `netblocker` `duel` `spawnkill` `geolocation` `location` `countryfilter` `poweradminurt` `codam` `poweradmincod7` `poweradminbf3` `poweradminbfbc2` `poweradminmoh` `poweradminhf` `urtserversidedemo` `jumper` | **Bundled.** Same name, YAML config |
| `chatlogger` | **A separate repository**: `b3 plugin install …` |
| `ftpytail` `sftpytail` `httpytail` `cod7http` | **Core.** Put the URL in `server.game_log` and remote tailing just happens |
| `scheduler` | **Core.** Cron is a service plugins register with |
| `pluginmanager` | **Core.** `b3 plugin` on the command line, `!plugin` in the game |
| `punkbuster` | **Core.** Plus `!pbss` and `!punkbuster` |
| `geowelcome` | **Two message variants on `welcome`** |
| `censorurt` | **A `mute:` section on `censor`** |
| `radio_spam_protection` | **A `radio:` section on `spamcontrol`** |
| `publist` `adv` | **Gone.** Both called `bigbrotherbot.net` services that have been unreachable for years |
| `translator` | **Dropped on purpose** — see the [reasoning](../README.md#what-was-deleted-and-what-was-rebuilt) |
| `xlrstats` | **Not ported.** 2,628 lines with its own web front end; it belongs on the web API rather than in a plugin |
| `webapi` | **Planned as a core layer**, not a plugin |

If you wrote your own plugin, it will not load unchanged — the plugin API is different (a typed
`@command` registry, `@handles` for log lines, an asyncio bus rather than threads). [Plugins](plugins.md)
has what writing one looks like now.

## 6. Run it

```bash
b3 run
```

Keep the old bot stopped while the new one runs — two bots on one game server will both answer every
command.

---

## Things that are deliberately different

Worth knowing before you decide something is broken.

* **`!ban` is temporary out of the box** — `ban_duration`, 14 days by default, exactly as in the
  classic. `!permban` is the permanent one. This trips people up in both versions.
* **No `b3/extplugins` folder.** Plugins are installed from git and pinned to a semver tag, because
  the forum they used to be shared on is gone.
* **One YAML file, validated.** A typo is reported with the key and the line, at startup, rather than
  becoming a default you did not choose.
* **No threads.** The classic ran five, plus a timer per plugin. If you were tuning `delay` or
  `lines_per_second`, there is nothing to tune.
* **Migrations.** Schema changes are applied with `b3 db upgrade`, and `b3 run` refuses to start
  against a database that is behind the code rather than failing later in a query.
* **A masked admin is masked in the `status` output too.** The classic's was not, which made `!mask`
  ineffective on any community that published its status page.

## If something goes wrong

`b3 doctor` first — it checks the config, the database, the schema revision, the log file and the
rcon connection, and says which one failed. If the bot is running but not *seeing* anything, `b3
probe` shows what your server actually replies, which pattern matched it, and the log lines nothing
read. Both are in [CLI](cli.md).
