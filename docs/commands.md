| [Overview](../README.md) | [CLI](cli.md) | [Plugins](plugins.md) | [Deployment](deployment.md) | **Commands** | [Configuration](configuration.md) | [Games](games.md) | [Development](development.md) |
|---|---|---|---|---|---|---|---|

# In-game commands

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
| `!pbss <player>` |  | 60 | Ask PunkBuster for a screenshot of what a player is seeing |
| `!spank <player> [reason]` | `!sp` | 60 | Kick a player, loudly |
| `!unban <player> [reason]` |  | 60 | Lift every active ban on a player (works offline, by @id) |
| `!banall <pattern> [reason]` | `!ball` | 80 | Ban every player whose name matches |
| `!clear [player]` | `!kiss` | 80 | Clear a player's warnings, or everyone's |
| `!clientinfo <player>` |  | 80 | Show a player's stored identity, level and history |
| `!kickall <pattern> [reason]` | `!kall` | 80 | Kick every player whose name matches |
| `!lookup <player>` | `!l` | 80 | Find a player in the database, connected or not |
| `!makereg <player>` | `!mr` | 80 | Make a player a regular |
| `!map <name>` |  | 80 | Change to another map — a partial name will do. Some engines take more; see below |
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
| `!punkbuster <command>` | `!pbcmd` | 100 | Hand a line to PunkBuster and show what it says |
| `!runas <player> <command>` | `!su` | 100 | Run a command as someone else |
| `!unmask [player]` |  | 100 | Stop hiding a level |

That is **all 59 of the classic bot's admin commands**, at its `plugin_admin.ini` default levels
(guest 0, user 1, reg 2, mod 20, admin 40, fulladmin 60, senioradmin 80, superadmin 100) — plus
`!pbss` and `!punkbuster`, which the classic offered through a separate `punkbuster` plugin and
through a copy in each of its three Frostbite plugins rather than as core commands.

`<player>` resolves as a slot id (`3`), a case-insensitive partial name (`bo` → Bob), or a database
id (`@42`). Commands that act on people who have already left — `!unban`, `!baninfo`, `!aliases`,
`!warns`, `!warnclear`, `!clientinfo`, `!putgroup`, `!ungroup`, `!makereg`, `!unreg`, `!leveltest`,
`!seen`, `!lookup` — also search stored names **and past aliases**, so someone who renamed themselves
is still findable.

**A name that could mean two people is refused, never guessed.** With "bob" and "bobby" both in the
server, `!ban bob` lists both with their slot numbers and does nothing until you say which; the same
goes for stored players, listed with their `@id`. An exact name always wins outright, so "bob" is
still one word even while "bobby" is playing.

`!map` resolves partial names the same way, against the server's own rotation — `!map metro` finds
`MP_Subway`. On Battlefield and Medal of Honor titles you can type the name you see on screen
(`!map grand bazaar`), and `!maps`, `!nextmap` and `!map` all answer with those names rather than the
engine's ids.

**Two engines take more than a map name, and `!map` takes it in the form their own server does:**

| Titles | Form | Example |
|---|---|---|
| `bf3` `bf4` `bfh` `mohw` | `!map <map>, [gamemode], [rounds]` | `!map grand bazaar, RushLarge0, 3` |
| `insurgency` | `!map <map> [gamemode]` | `!map market push` |

A comma on Frostbite because its map names contain spaces; a space on Insurgency because that is
what `changelevel` itself takes. Both extras are optional — leave one out and the server keeps what
it is running. Everywhere else the whole line is the map name, spaces and all, and typing more than
the engine understands gets the usage line rather than a map loaded in the wrong mode.

`!pbss <player>` (level 60) asks PunkBuster for a screenshot of what a player is seeing. It only
works where the server is running PunkBuster — the bot asks at startup — and the picture is saved
in PunkBuster's own folder **on the game server**, not sent back to the admin who asked.

`!punkbuster <command>` (level 100, `!pbcmd`) hands a line straight to PunkBuster and shows the
reply: `!punkbuster pb_sv_plist`, `!punkbuster PB_SV_BanGuid …`, anything the anti-cheat takes. It is
superadmin because `PB_SV_*` covers the ban list, the config file and PunkBuster's own settings. How
the line reaches PunkBuster is the title's business, not yours — on Quake 3 and Call of Duty servers
a `PB_SV_*` verb is an RCON command in its own right, and on Battlefield it is an argument to
`punkBuster.pb_sv_command`. Where the server is not running PunkBuster the command says so rather
than sending the line into the dark.

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

## Groups, reach and masking

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

## Rules, spam messages and reason keywords

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

## What warnings add up to

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

## Knowing who is really on the server

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
