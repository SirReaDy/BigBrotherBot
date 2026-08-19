# Big Brother Bot (B3) 2.0

A modern, typed, `asyncio`-based rewrite of [Big Brother Bot](https://github.com/BigBrotherBot/big-brother-bot),
the game-server admin bot: it watches a game server, keeps a database of who played and what they did,
and gives your admins commands to act on it.

| **Overview** | [CLI](docs/cli.md) | [Plugins](docs/plugins.md) | [Deployment](docs/deployment.md) | [Commands](docs/commands.md) | [Configuration](docs/configuration.md) | [Games](docs/games.md) | [Development](docs/development.md) |
|---|---|---|---|---|---|---|---|

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

**Where it stands today.** All 59 classic admin commands at the classic levels, 38 game titles across
ten engine families — 34 of the classic bot's 37, plus four it never had — and every core service the
old bot offered, plus remote log tailing, PunkBuster, per-server deployment and pre-flight checks it
never had. 1,771 tests, `mypy --strict` clean.

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
pytest
cd examples && python -m b3.cli -c b3.yaml replay games_mp.log   # offline replay demo
```

The replay demo boots the bot against a recorded CoD4 log: a player claims superadmin with
`!iamgod`, then bans another player — issuing RCON and recording the penalty in SQLite.

---

## Documentation

| Page | What is in it |
|---|---|
| [CLI](docs/cli.md) | Every `b3` command: `run`, `probe`, `init`, `db`, `plugin` |
| [Plugins](docs/plugins.md) | What ships in the box, and how to write an installable one |
| [Deployment](docs/deployment.md) | One bot per game server, several on one machine, systemd, tailing a hosted server's log |
| [Commands](docs/commands.md) | All 59 in-game commands, groups and reach, warnings, spam |
| [Configuration](docs/configuration.md) | `b3.yaml` in full, how plugins load, message overrides, scheduling |
| [Games](docs/games.md) | What each engine needs: Frostbite, BattlEye, Source, Altitude, Homefront, Ravaged, Frontline, the CoD variants |
| [Development](docs/development.md) | The data model inherited from the classic bot, and how to work on this one |

---

## Supported games

Ten engine families, thirty-eight titles — `b3 games` prints the list on any install. Set `server.game`
to one of them:

| Family | `server.game` |
|---|---|
| **Call of Duty** | `cod` `cod2` `cod4` `cod4x` `cod4gr` `cod5` `cod6` `cod7` `cod8` |
| **Call of Duty — Plutonium** | `plutoiw5` (MW3) `plutot6` (Black Ops 2) |
| **BattlEye** | `arma2` `arma3` |
| **Frostbite** | `bfbc2` `moh` `bf3` `bf4` `bfh` `mohw` |
| **Quake 3** | `q3` (also accepted as `q3a`) `oa081` `smg` `smg11` `wop` `wop15` |
| **Quake 3 — Urban Terror** | `iourt41` `iourt42` `iourt43` |
| **Quake 3 — Enemy Territory** | `et` `etpro` |
| **Quake 3 — Soldier of Fortune 2** | `sof2` `sof2pm` |
| **Altitude** | `altitude` |
| **Homefront** | `homefront` |
| **Ravaged** | `ravaged` |
| **Frontlines: Fuel of War** | `frontline` |
| **Source** | `insurgency` `cs2` |

A family is one parser; the titles in it are data — GUID length, ban verbs, the shape of the status
table. Adding a title to a family is a few lines; adding a family is a parser.

The two families differ in one structural way worth knowing. A CoD log repeats a player's identity
on every line; a Quake3 log states it **once**, in an infostring when the player connects, and
every later line is a bare slot number. A chat line names the speaker by name, or by slot and name
on the titles that write both — where the two disagree the name wins, because some of these engines
report chat against the wrong slot and attributing a `!command` to the wrong player is how the wrong
person gets banned.

Capture-the-flag, awards, item pickups and generic objectives all arrive as events on every title in
this family. Beyond that, the Urban Terror / ET / SoF2 / World of Padman 1.5 rows are the shared
grammar **plus** that title's own lines, read by a subclass — a title never loses a line by gaining a
family of its own:

- **Urban Terror** — `Hit:` (non-fatal hits with a hit location, which is what makes team-damage
  policing possible), `Radio:`, private chat, spawns, kill assists, flags, capture times, bomb mode
  and the voting lines. A hit line carries no damage figure, so the damage comes from a
  weapon×hit-location table, and hit lines number weapons on a *different* scale from kill lines.
  Radio is policed by `spamcontrol` like any other chat channel. Names longer than the protocol
  allows are truncated, and by default the player is kicked — that length is an exploit rather than a
  nickname; `server.allow_long_names: true` keeps the truncation and lets them play. Not included:
  UrT's `auth` account service, and the freeze-tag and jump-run lines (they belong with the plugins
  that want them).
- **Enemy Territory** — `ConnectInfo:`, which is where an ET player's identity comes from: a plain ET
  server sets no `cl_guid` at all, so without this line nobody could hold a level or a ban. Also
  `Gib:` and ET's own slot-numbered chat lines, which beat matching a colour-coded name.
- **Soldier of Fortune 2** — its `Hit:` line, which states the damage outright.
- **World of Padman 1.5** — its own kill line (`Kill: <attacker> <means-of-death> <victim>`, a
  different shape from every other title here) and its `Damage:` line for non-fatal hits.

---

## Requirements

- Python 3.11+
- `git` on `PATH` (only for `b3 plugin install/update`)
- No system time-zone database needed; `tzdata` is installed as a dependency

## License

GPL-2.0-or-later, inherited from the original B3 project.
