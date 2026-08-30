| [Overview](../README.md) | **CLI** | [Plugins](plugins.md) | [Deployment](deployment.md) | [Commands](commands.md) | [Configuration](configuration.md) | [Games](games.md) | [Development](development.md) | [Migrating](migrating.md) |
|---|---|---|---|---|---|---|---|---|

# The `b3` command line

Every command takes `-c/--config <path>` (default `b3.yaml`) and `-v/--verbose` (debug logging).
Installed as `b3`; equivalently `python -m b3.cli`.

## Running

| Command | What it does |
|---|---|
| `b3 init <dir>` | Create a bot instance for one game server — config, plugin config, `plugins/`, optional systemd unit. See [Running B3](deployment.md) |
| `b3 import-config <conf-dir>` | Convert a classic B3 `conf/` directory into this one's YAML, reporting everything it would not convert. Needs no config — it is how you get one. [Below](#converting-a-classic-installs-config) |
| `b3 -c b3.yaml doctor` | Check this install before starting it: RCON, game log, database, schema, plugins, updates |
| `b3 update --check` | Is there a newer version? (exits 1 if so) |
| `b3 -c b3.yaml probe` | Show what this server actually says — the raw `status` reply, which row pattern matched it, the parsed players, and **which log lines this bot does not understand**. Read-only; `--redact` masks addresses and ids for pasting somewhere public |
| `b3 -c b3.yaml run` | Connect to the server, tail the game log (locally or [over the network](deployment.md#tailing-a-hosted-servers-log)), and run until stopped |
| `b3 -c b3.yaml replay <logfile>` | Replay a recorded log offline — no server, no RCON. The test/demo harness |
| `b3 games` | Every valid `server.game`, grouped by engine, marking which need no game log. Needs no config |
| `b3 version` | This version and the newest released one, side by side. Needs no config |
| `b3 completion [shell]` | Print the one line that gives your shell tab completion for all of this. Needs no config |
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

### Converting a classic install's config

```
b3 import-config <classic conf/ directory> [-o DIR] [--dry-run]
```

`b3 init` is for a server you are setting up; this is for one that has been running for years.
It reads the classic `b3.xml` and every `plugin_*.ini` (or `.xml`) beside it, writes `b3.yaml` and a
`plugin_*.yaml` per plugin, and **prints what it would not convert with what to do about each item**.
Needs no config — it is what you run to produce one. `--dry-run` prints the report and writes nothing.

The report is the feature. Settings are checked key by key against the plugin's own defaults, so
anything renamed, restructured or dropped is named rather than written through as a key the plugin
would ignore — which would leave a value you had tuned sitting silently at its default. Sections that
moved (`[commands]` is the `cmdmanager` plugin now, `[messages]` is `b3.yaml`) and plugins that are
core services here are reported with their destination. Free text — your rules, warning reasons,
bad-word lists and custom commands — is copied whole, because it is your writing and there is nothing
to check it against.

[Migrating](migrating.md) is the full walkthrough, database included.

## Tab completion

None of the values above are guessable. There are 38 game ids, thirty-odd bundled plugins, a config
path per server and a migration revision that only exists in the scripts — and until now the only way
to see any of them was `b3 games`, `b3 plugins`, or the source. Tab is where somebody looks first.

```console
$ b3 completion bash >> ~/.bashrc     # then start a new shell
$ b3 init srv --game bf<TAB>
bf3  bf4  bfbc2  bfh
$ b3 plugin enable power<TAB>
poweradminbf3  poweradminbfbc2  poweradmincod7  poweradminhf  poweradminmoh  poweradminurt
```

`b3 completion` **prints**; it does not install. Editing somebody's shell configuration behind their
back is not something a game-server bot should do, and every shell keeps its own file with its own
conventions — one line an operator can read before pasting is both smaller and honest. With no shell
named it guesses from `$SHELL` and says which it guessed, because a bash snippet pasted into zsh fails
silently and leaves you wondering.

| Shell | Where the line goes |
|---|---|
| `bash` | `~/.bashrc` |
| `zsh` | `~/.zshrc` |
| `fish` | `~/.config/fish/completions/b3.fish` |
| `tcsh` | `~/.cshrc` |
| `powershell` | your `$PROFILE` |

PowerShell is on that list, which this project's own notes had said Windows had no answer for: modern
`argcomplete` registers a native `ArgumentCompleter` for it, so Windows gets the same completions as
everywhere else.

What completes, and where each list comes from — none of it restated anywhere, which is why a title or
a plugin added tomorrow completes without anybody remembering:

| Where you press tab | What you are offered | Read from |
|---|---|---|
| `-c/--config` | `*.yaml`/`*.yml`, and directories with a trailing `/` so a second tab walks in | the filesystem |
| `--game` | every title the bot reads | the profile table |
| `b3 plugin enable/disable/update <name>` | bundled plugins **and** the ones installed on this machine | the plugin package, `conf/plugins/`, `plugins/` |
| `b3 plugin remove <name>` | only what was installed — a bundled name is what that command exists to reject | `conf/plugins/`, `plugins/` |
| `b3 db … --revision` | every revision, plus `head` and `base` | the packaged migration scripts — no database, which is exactly when you are typing a revision by hand |

Completion is an extra, not a dependency: `pip install 'b3ng[completion]'`. Without it the bot behaves
identically and `b3 completion` says that one line instead of a traceback — a bot that would not start
without a completion library is a poor trade. Registration runs `b3` itself to ask what comes next, so
it works with the layout [deployment](deployment.md) recommends, where `b3` is a console script inside
a virtualenv symlinked onto `PATH`.

## Updates

| Command | What it does |
|---|---|
| `b3 version` | Print this version and the latest release. Always exits 0 — reading a version must not fail a script because a release exists. `--refresh` asks the remote instead of using the answer from the last week |
| `b3 update --check` | Ask now, print the answer, change nothing. Exits **1** when an update exists, 0 when current, 2 when the check failed — so a cron job can mail you and a script can tell those apart |
| `b3 update` | Show current → latest and install it, after asking |
| `b3 update -y` | The same without the question, for a script |
| `b3 update --to v2.0.4` | Install a specific tag. This is also how you roll **back**, and the only way to install a pre-release |

**You do not have to ask.** After any command finishes, one line goes to stderr when a newer release
exists:

```console
$ b3 -c b3.yaml plugins
... the command's own output ...
b3 2.1.0 is available (running 2.0.0) — run `b3 update` to install it
```

That line costs nothing, which is the only reason it is acceptable to print it at all. It **reads a
remembered answer** — a small JSON file in your cache directory (`~/.cache/b3/update.json`, or
`%LOCALAPPDATA%\b3\update.json` on Windows) written by whatever asked last: the running bot's daily
check, `b3 doctor`, or `b3 version`. Only when that answer is more than **a week** old does a command
pay for a `git ls-remote`, with a five-second timeout, *after* it has finished its own work — so
nothing you type is ever waiting on the network before it answers you.

Four things it will not do: interrupt output (it comes last, on stderr, so `b3 games | grep cod` is
unaffected); talk to a pipe (only a terminal gets it — a cron job that wants this uses
`b3 update --check`, whose exit code is built for exactly that); repeat itself on the commands that
already report it (`update`, `version`, `doctor`, `run`, `completion`); or say anything when there is
no update, because being current is not news. `B3_NO_UPDATE_NOTICE=1` silences it for a shell, and
`bot.update_check: false` switches checking off entirely.

**Only final releases are offered.** A `v2.1.0-rc1` or `v2.2.0b1` tag is a real tag somebody meant to
push, and `b3 update --to v2.1.0-rc1` installs one on purpose — but neither this line nor
`update --check` will ever name one, because a candidate that could be offered would go on being
offered after the release it was a candidate for had shipped. The same ordering runs the other way:
running `2.0.0a0`, you are offered `2.0.0` when it lands.

The bot also asks by itself while it runs, at most once per `bot.update_check_interval` (a day by
default), and says something **only when there is an update**. `b3 doctor` shows the same answer as an
`update` row, and `!b3` mentions it in game — none of them waits on the network, because all of them
read the answer some earlier check already wrote down.

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
