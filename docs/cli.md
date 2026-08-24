| [Overview](../README.md) | **CLI** | [Plugins](plugins.md) | [Deployment](deployment.md) | [Commands](commands.md) | [Configuration](configuration.md) | [Games](games.md) | [Development](development.md) |
|---|---|---|---|---|---|---|---|

# The `b3` command line

Every command takes `-c/--config <path>` (default `b3.yaml`) and `-v/--verbose` (debug logging).
Installed as `b3`; equivalently `python -m b3.cli`.

## Running

| Command | What it does |
|---|---|
| `b3 init <dir>` | Create a bot instance for one game server — config, plugin config, `plugins/`, optional systemd unit. See [Running B3](deployment.md) |
| `b3 -c b3.yaml doctor` | Check this install before starting it: RCON, game log, database, schema, plugins, updates |
| `b3 update --check` | Is there a newer version? (exits 1 if so) |
| `b3 -c b3.yaml probe` | Show what this server actually says — the raw `status` reply, which row pattern matched it, the parsed players, and **which log lines this bot does not understand**. Read-only; `--redact` masks addresses and ids for pasting somewhere public |
| `b3 -c b3.yaml run` | Connect to the server, tail the game log (locally or [over the network](deployment.md#tailing-a-hosted-servers-log)), and run until stopped |
| `b3 -c b3.yaml replay <logfile>` | Replay a recorded log offline — no server, no RCON. The test/demo harness |
| `b3 games` | Every valid `server.game`, grouped by engine, marking which need no game log. Needs no config |
| `b3 -c b3.yaml plugins` | Every plugin available here — bundled, installed for this server, installed in the shared pool — and which ones this config runs |

`init` flags: `--name`, `--game`, `--host`, `--port`, `--rcon-password`, `--game-log`,
`--command-file` (Altitude only), `--database`, `--shared-plugins-dir`, `--service` (write a systemd
unit), `--service-user`, `--force`.

`--game` only accepts a title the bot actually reads, and so does the config: an unrecognised
`server.game` stops the bot at startup with the near match named (`unknown game 'bf3_typo' — did you
mean 'bf3'?`) rather than falling back to a parser that would silently match nothing.

## Asking a server what it really looks like

```
b3 -c b3.yaml probe [--lines 200] [--cvar sv_maxclients] [--redact]
```

`doctor` answers "is this install working?" with a verdict. `probe` answers "what does this server
actually say?" with evidence, and it is the command to run when something is not recognised — or when
somebody here asks you what your server prints. It shows:

- the **raw** reply to every status command this title has, exactly as it arrived;
- how many rows each candidate row pattern matched, named by the columns it expects — so "the shape
  with a Steam64 column matched 0 rows and the one without matched 4" tells you the server's setting
  at a glance;
- the players that came out, and which column was taken as their identity;
- the reply to one cvar read, in the form this title asks for it;
- and the part worth the most: **how many log lines matched no handler, with examples.** A line this
  bot does not understand looks exactly like a line the server never wrote, so this is the only way to
  see a grammar gap rather than infer one from silence.

It is strictly read-only — status, one cvar, and the tail of the log; there is no path from it to a
kick, a ban or a line of chat. `--redact` masks addresses and ids (keeping map names, which are the
useful part) for pasting into an issue or a forum thread.

## Database

Schema is managed by Alembic; a fresh database is created and stamped automatically on first run.

| Command | What it does |
|---|---|
| `b3 db current` | Show the applied migration revision |
| `b3 db head` | Show the latest available revision |
| `b3 db upgrade [--revision REV]` | Apply pending migrations (default: `head`), reporting `0001 -> 0003` and which it applied |
| `b3 db stamp [--revision REV]` | Mark the database as being at a revision without running it |
| `b3 import-db <sqlalchemy-url>` | Import a legacy (Python-2 era) B3 database |

**After updating the bot, run `b3 db upgrade` for each config.** A *fresh* install is always right —
the schema is created and stamped — but an upgraded one is not: the code moves to a revision the
database has not applied, and the only symptom is whatever the new column was for failing oddly. So
the bot **refuses to start** when its database is behind, naming the revisions and the command:

```
ERROR database at 0001, code expects 0003 — missing 0002, 0003
ERROR -> run: b3 -c b3.yaml db upgrade
ERROR -> or start with --allow-schema-drift if you know this database is fine
```

`b3 doctor` reports the same thing as a `schema` row before you get there. A database that is
*ahead* — a shared one a newer bot has already migrated — is a warning rather than a refusal: an
older bot writing to a newer schema may quietly stop populating new columns, but taking a working
server down over somebody else's upgrade would be worse. `b3 run --allow-schema-drift` starts anyway
when you know better.

Legacy import preserves client ids, group bitmasks, epoch timestamps, and penalty semantics;
dangling `admin_id` references are nulled and orphaned penalties/aliases are skipped (the old
MyISAM schema had no foreign keys), with a summary report of exactly what was imported or skipped.

## Setting a server up

`b3 init <directory>` creates one game server's instance directory: the config, an example config per
plugin, `plugins/`, and optionally a systemd unit.

**With no `--game` and a terminal to ask at, it asks.** That is the point of the interactive half: a
command that only takes flags cannot help somebody who has not read the flags yet, and `b3 init` is
the first command a new operator runs. The questions are in the order an operator thinks in — which
game, where it is, how to talk to it, where its log is, where to keep the data, what to run — and each
answer is checked **as it is given**: the game against the list of titles (with the nearest match when
it is a typo), the port as a port, the log as a file that exists (or an `ftp://`/`sftp://`/`http://`
URL), and the database by *opening* it, which is the only way to find a missing driver before the
first start rather than after it.

Nothing is written until the last question is answered, so Ctrl-C leaves no half-made directory. It
then offers to run `b3 doctor` straight away, because a config being written is not the same thing as
a server being reachable, and the gap between those two is where a first evening goes.

| Command | What it does |
|---|---|
| `b3 init srv` | Ask the questions, then write `srv/` |
| `b3 init srv --game cod4 --rcon-password pw …` | Take the flags and write it — never asks |
| `b3 init srv --interactive` | Ask even though flags were given |
| `b3 init srv --no-interactive` | Never ask, even with no `--game` |

A scripted `b3 init` — in a Dockerfile, in a provisioning script — never stops to ask: with `--game`
given, or with nothing attached to a terminal, it takes the flags and their defaults.

## Updates

| Command | What it does |
|---|---|
| `b3 update --check` | Ask now, print the answer, change nothing. Exits **1** when an update exists, 0 when current, 2 when the check failed — so a cron job can mail you and a script can tell those apart |
| `b3 update` | Show current → latest and install it, after asking |
| `b3 update -y` | The same without the question, for a script |
| `b3 update --to v2.0.4` | Install a specific tag. This is also how you roll **back** |

The bot also asks by itself, at most once per `bot.update_check_interval` (a day by default), and says
something **only when there is an update**. `b3 doctor` shows the same answer as an `update` row, and
`!b3` mentions it in game — neither of them waits on the network, because both read the answer the
scheduled check already has.

This replaces the classic bot's update machinery, which was deleted deliberately, and the differences
are the point: it reads **a repository you name** (`bot.update_remote`, empty to switch the feature
off) rather than a domain the project stopped controlling; it asks once a day rather than on every
startup; a *check* never installs anything; and it sends nothing about your server — `git ls-remote`
reads public tags and does not report who is asking. A private repository takes a token from
`B3_UPDATE_TOKEN` in the environment, never from the config file, and it is redacted from anything
printed.

Two things `b3 update` says before it acts, because both surprise people:

- **One code install serves every instance on the machine**, so this updates all of them at once.
- **Nothing is updated in place.** Files change; the change takes effect when you restart each bot —
  and then `b3 -c <config> db upgrade`, since a new version may add a migration the bot will refuse to
  start without.

Inside a container it does not try: the image *is* the version, so it says to pull a new one.

## Plugins

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

See [Running B3](deployment.md) for the whole layout.
