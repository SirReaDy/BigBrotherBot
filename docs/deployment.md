| [Overview](../README.md) | [CLI](cli.md) | [Plugins](plugins.md) | **Deployment** | [Commands](commands.md) | [Configuration](configuration.md) | [Games](games.md) | [Development](development.md) |
|---|---|---|---|---|---|---|---|

# Running B3: one bot per game server

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

## One game server

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

This is also the layout tab completion is written for: the symlink above means `b3` on `PATH` is a
console script inside a virtualenv, and `b3 completion <shell>` prints a registration that runs `b3`
itself to ask what comes next, so it works there. `pip install 'b3ng[completion]'`, then
[the CLI page](cli.md#tab-completion).

## Several game servers on one machine

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

## Adding a server later

Nothing to reconfigure: `b3 init` the new directory, `doctor` it, install its unit. The existing bots are
untouched — they do not know about each other.

## What lives where, and why

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

## Check before starting — the step that saves the evening

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

## Sharing plugins between servers

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

## Sharing players and bans between servers

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

## The bot need not run on the game server

`server.game_log` accepts `ftp://`, `ftps://`, `sftp://` and `http(s)://` URLs, so one VPS can run every
bot while the game servers live elsewhere — the log is tailed over the network and RCON is a network
protocol anyway. See [Tailing a hosted server's log](#tailing-a-hosted-servers-log).

On Arma and Battlefield titles there is no log at all — the RCON connection carries the events — so those
bots run anywhere that can reach the RCON port, and `server.game_log` is ignored entirely.

## Without systemd

The unit file is a convenience, not a requirement: `b3 -c <config> run` is an ordinary foreground
process. Anything that restarts a process will do, provided it treats **exit code 221 as "start me
again"** (that is `!restart`) and **0 as "stay stopped"** (that is `!die`). On Windows, use one scheduled
task or service per instance directory, each with its own `-c`.

## In a container

There is a `Dockerfile` in the repository. It builds one image that runs any of the 38 titles — the
image carries the code, the instance directory carries everything about a server:

```bash
docker build -t b3ng .
docker run -d --name cod4_1 --restart unless-stopped   -v /srv/cod4_1/b3:/data   b3ng
```

`/data` is the instance directory this page has been describing all along: `b3.yaml`, the database and
`conf/plugins/` live there, on the host, so upgrading is `docker pull` (or a rebuild) and nothing about
a server is inside the image. Run one container per game server, exactly as you would run one systemd
unit per game server, and give each its own directory.

Anything that is not `run` works too, because the entry point is the bot's CLI with the config already
pointed at:

```bash
docker run --rm -v /srv/cod4_1/b3:/data b3ng doctor
docker run --rm -v /srv/cod4_1/b3:/data b3ng db upgrade
```

The container runs as a non-root user with uid 10001, so the instance directory has to be writable by
it: `chown -R 10001:10001 /srv/cod4_1/b3`.

**`b3 update` does not work in a container, and says so.** The image is the version: a `pip install`
inside a container is thrown away with the container. Build or pull a new image instead.

## Tailing a hosted server's log

`server.game_log` is normally a path, which means the bot runs on the game server's own box. Point it
at a URL instead and it tails the log over the network — the standard way to run against a **hosted**
server you have no shell on (the classic bot's `ftpytail` / `sftpytail` / `httpytail` plugins).

| Scheme | Notes |
|---|---|
| *(a path)* | Local file. No polling delay, no extra dependencies. |
| `ftp://user:pass@host[:port]/path/games_mp.log` | Resumes with `REST`, binary mode, passive (game hosts are behind NAT). |
| `ftps://…` | Same, over implicit TLS, with the data connection encrypted too. |
| `sftp://user:pass@host[:port]/path/games_mp.log` | Needs `paramiko`: `pip install b3ng[sftp]`. With no password in the URL it uses your agent and `~/.ssh` keys. An unknown host key is accepted with a warning. |
| `http://…`, `https://…` | Polls `HEAD` for the size, then a ranged `GET`. Credentials in the URL become a basic-auth header. Servers that ignore `Range` still work. Asks for **no compression** — see below. |

All of them tail by **byte offset**, so:

- Only new bytes are transferred; the offset survives a reconnect, so nothing is replayed or lost.
- A log that shrinks was rotated or truncated — reading restarts from the top.
- A gap bigger than `log_max_gap` (first poll, or a rotation you missed) is skipped rather than
  replayed, so the bot never acts on a flood of stale events. It resumes at the next full line.
- A dropped connection is retried with exponential back-off (5s, doubling, capped at 5 minutes) and
  logged; the bot keeps running. Passwords are redacted from every log line.
- A wrong host, path or password fails loudly at startup instead of quietly tailing nothing.

Percent-encode any `@` or `:` in a username or password (`p@ss` → `p%40ss`).

**HTTP and compression.** Because the tail is by byte offset, the HTTP source sends
`Accept-Encoding: identity` and means it. A compressed reply counts *compressed* bytes — in the
`Content-Length`, in the `Range`, and in the offset kept between polls — while the text handed to the
parser is the decompressed kind, and nothing about that mismatch fails loudly: it reads as a log that
has gone strange. If a host compresses anyway, the bot handles what it actually sent. A whole file
(the server ignored `Range` too) is decompressed and sliced, because then the offsets are against the
complete file again. A **compressed partial** response is refused by name in the log, because its
range is a position in a stream that cannot be mapped back to the file — serve the log uncompressed,
or turn range requests off so the whole file is sent.
