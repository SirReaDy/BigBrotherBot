| [Overview](../README.md) | [CLI](cli.md) | **Plugins** | [Deployment](deployment.md) | [Commands](commands.md) | [Configuration](configuration.md) | [Games](games.md) | [Development](development.md) |
|---|---|---|---|---|---|---|---|

# Plugins

## Bundled plugins

`admin` is always there. These are ported from the classic tree and switched on in the `plugins:`
list, each with an optional config of its own (see `examples/`):

| Plugin | What it does | Adds |
|---|---|---|
| `admin` | the 59 commands, groups, warnings | — |
| `censor` | bad language in chat, bad player names, with an optional escalating mute | — |
| `spamcontrol` | scores chat and warns whoever floods it, and mutes whoever floods the radio | `!spamins [player]` |
| `pingwatch` | removes players whose connection is spoiling the game | `!ci <player>` |
| `tk` | team damage points, forgiving, and a ban for whoever will not stop | `!forgive` `!fp` `!forgiveall` `!forgivelist` `!forgiveinfo` `!forgiveclear` `!grudge` |
| `stats` | kills, deaths, damage, a relative skill score and XP, per session | `!mapstats` `!stats` `!testscore` `!topstats` `!topxp` |
| `welcome` | greets arrivals with their history and where they are from, and announces their own greeting | `!greeting [text\|none]` |
| `afk` | asks players who look absent whether they are, and removes the silent ones | — |
| `banlist` | applies ban lists from a file or a URL, with whitelists that win | `!banlistinfo` `!banlistupdate` `!banlistcheck` |
| `cmdmanager` | command levels, aliases and per-player grants, at runtime | `!cmdlevel` `!cmdalias` `!cmdgrant` `!cmdrevoke` `!cmduse` |
| `customcommands` | commands you define in config, sending rcon lines of your own | whatever you name |
| `status` | writes the roster and server state to a file (or FTP, or tables) | — |
| `login` | admins must type a password before their level does anything | `!login` `!setpassword` |
| `ipban` | kicks a connecting player whose address is behind an active ban | — |
| `nickreg` | reserves nicknames, and warns whoever else is wearing one | `!registernick` `!deletenick` `!listnick` |
| `makeroom` | frees a slot on a full server, and holds it for a member | `!makeroom` `!makeroomauto` |
| `callvote` | decides who may call a vote, protects your admins from kick votes, and records the ones that finish | `!veto` `!lastvote` |
| `spree` | announces killing and losing streaks | `!spree [player]` |
| `firstkill` | announces first blood, first headshot and the first team kill | `!firstkill` `!firsttk` `!firsths` |
| `netblocker` | refuses players connecting from a listed network | — |
| `duel` | keeps score between two players who agreed to a duel | `!duel` `!duelreset` `!duelcancel` |
| `spawnkill` | warns, kicks or bans players who shoot somebody who just spawned | — |
| `geolocation` | resolves where a player is connecting from, for the plugins that need it | — |
| `location` | announces where arrivals are from, and answers for it | `!locate` `!distance` `!isp` |
| `countryfilter` | refuses players from the countries a server does not accept | — |
| `poweradminurt` | Urban Terror's admin commands, and the two balancers | `!paslap` `!panuke` `!pakill` `!pamute` `!pateams` `!pabalance` `!paskuffle` `!paforce` `!pabigtext` `!paset` `!paget` `!pavote` `!pactf` `!pabomb` `!pagear` … |
| `codam` | a Call of Duty admin mod's own verbs, from a list you write | `!codam` plus `c` + every verb you list |
| `poweradmincod7` | Black Ops's playlists, map exclusions, DLC packs and config files | `!pasetmap` `!paplaylist` `!pagetplaylists` `!pasetplaylist` `!paexcludemaps` `!paset` `!paget` `!pasetdlc` `!palistcfg` `!paload` `!pamaprestart` `!pafastrestart` `!pagametype` |
| `poweradminbf3` | Battlefield 3's teams, scrambler, VIP list and preset server configs | `!roundnext` `!endround` `!kill` `!nuke` `!changeteam` `!swap` `!autoassign` `!autobalance` `!scramble` `!setnextmap` `!yell` `!vips` `!loadconfig` … |
| `poweradminbfbc2` | Bad Company 2's teams, playlists, yells and ready-up match mode | `!pateams` `!pateambalance` `!pachangeteam` `!paspectate` `!pakill` `!pamatch` `!ready` `!pamaprestart` `!pasetnextmap` `!parush` `!payell` `!paset` `!paget` … |
| `poweradminmoh` | the 2010 Medal of Honor's teams, scrambler, match mode and reserved slots | `!teams` `!teambalance` `!changeteam` `!spect` `!swap` `!kill` `!scramble` `!scramblemode` `!autoscramble` `!match` `!ready` `!runnextround` `!setnextmap` `!reserveslot` … |
| `poweradminhf` | Homefront's teams, match mode and its three player verbs | `!pateams` `!pateambalance` `!paautobalance` `!pachangeteam` `!paspectate` `!pakill` `!pamatch` `!ready` `!payell` `!paident` `!panextmap` |
| `urtserversidedemo` | records a player from an Urban Terror 4.2 server, and tells you the filename | `!startserverdemo` `!stopserverdemo` |

### censor — bad language, and the escalating mute

A list of bad words, each a plain word or a regular expression, each able to carry its own penalty.
Patterns compile once at startup and a broken one is reported and skipped; a plain word matches on word
boundaries **in chat** (so "ass" does not fire on "class") and anywhere in a **name**, because
"xXcheaterXx" has no boundaries to find.

**Muting instead of warning** was a separate plugin in the classic bot — `censorurt`, a subclass of this
one. It is a `mute:` section here: minutes of silence escalating per offence, an optional slap, and a
`warn_after` after which the word's own penalty applies as well. It needs the engine to have a `mute`
verb (`GameProfile.player_verbs`, which today means Urban Terror); on any other title the section is
refused at startup with a reason rather than silently doing nothing.
Config: `examples/plugin_censor.yaml`.

### spamcontrol — chat, and the radio

Spam is scored rather than counted: saying the same thing twice is worse than saying two different
things, and a coloured repeat is worse still. Points decay over time, so an ordinary talkative player
never trips it while somebody pasting the same advert every second does, quickly. `!spamins [player]`
says how close somebody is.

**The radio is scored separately**, in a `radio:` section, on games that have one. That was a feature
of its own in the classic — `radio_spam_protection`, inside `poweradminurt`, and only in its Urban
Terror 4.2 subclass, so a 4.1 server never had it — but it belongs here, in the same way `censorurt`
turned out to be a `mute:` section on `censor`. Two things make the radio not chat. A radio message is
chosen from a fixed menu, so repeating one is ordinary and the content says almost nothing: a call is
scored on the **gap** since the last one, which is what marks out somebody abusing it. And it is
answered with a **mute** rather than a warning, because the radio is a menu of buttons — telling
somebody to stop does not stop them, and they are not reading the chat a warning arrives in. That needs
the engine's `mute` verb; where there is none the section is refused at startup with a reason and radio
spam is warned about like chat. Config: `examples/plugin_spamcontrol.yaml`.

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

**Where they are from.** If `geolocation` is loaded and has placed the player, the two server
announcements are replaced by variants naming their country — `announce_first_geo` and
`announce_user_geo`, with `{place}` available to every message here. That is the whole of what the
classic's separate `geowelcome` plugin did: it was a *subclass* of `welcome` that re-implemented the
greeting flow to add two messages and disabled `welcome` if it found it loaded. Nothing to switch on,
and `welcome` behaves exactly as before on a server with no geolocation.

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

### banlist — somebody else's ban list

Communities share ban lists, and this honours one without your admins retyping it. Four kinds: `ip`
(with the published `.0` range convention — an entry ending `.0` covers the last octet, `.0.0` two,
`.0.0.0` three), `guid`, `pbid` (a PunkBuster id, which is a different thing from a guid) and `roc`
("Rules of Combat" files). A list can be a local file, a URL cached into one, or both; URLs refresh
hourly with conditional requests, so a list nobody changed costs one round trip and no bytes.

Whitelists are checked first and win outright — that is the promise worth having, because it means a
list you do not control can never remove one of your own regulars. A player at or above
`immunity_level` gets an admin note rather than a kick.

Lists are checked at authentication **and when a player's address turns up**, which on the Call of Duty
and Quake 3 engines is not the same moment: their log lines carry no IP, and the status poll resolves
one seconds later. An IP list checked only at auth matches nobody at all on those titles.
Config: `examples/plugin_banlist.yaml`.

### cmdmanager — who may run what, without a restart

`!cmdlevel <command> [<level>]` moves a command; a level is a group keyword or a number, and
`mod-admin` sets a ceiling as well as a floor. `!cmdalias` gives it a shorter name, refusing one that
already belongs to another command. `!cmdgrant <player> <command>` is the one nothing else can do: it
gives **one player one command** without promoting them into a group that carries everything else with
it. `!cmdrevoke` takes it back and `!cmduse` answers "can they?" for anybody.

An admin can never change a command they cannot run themselves. Everything set this way lives in the
plugin's own tables, so it survives a restart *and* leaves your config files alone — the classic wrote
the new level back into the owning plugin's `.ini`, comments and all. Overrides are re-applied when a
plugin is enabled again, which would otherwise reset its commands to the levels in their code.
Config: `examples/plugin_cmdmanager.yaml` (documentation only — there is nothing to set).

### customcommands — your own commands, from config

Write a name, a level and the rcon line to send, and the bot registers a command like any other. The
line may carry placeholders: `<ARG>` (or `<ARG:OPT:default>`) for what the player typed,
`<ARG:FIND_PLAYER:PID>` for a player resolved the way every other command resolves one — also
`GUID`, `PBID`, `NAME`, `B3ID` — `<ARG:FIND_MAP>` for a map matched against the rotation, and
`<PLAYER:...>`, `<LAST_KILLER:...>`, `<LAST_VICTIM:...>` for the caller, whoever last killed them and
whoever they last killed. One argument per command, since that is all a command has.

**Everything substituted is sanitised.** The classic pasted a player's name into the rcon line
untouched, so a player called `bob"; quit` could turn an admin's `!slap` into two commands. A
placeholder this bot does not recognise is also refused when the command is *loaded* rather than sent
to the server as literal text — the classic's version of that was a command which looked configured
and did nothing on every use. Config: `examples/plugin_customcommands.yaml`.

### status — the roster, where a web page can read it

Every minute it writes what is happening — map, gametype, player count, and every player with their
level, score, ping and team — to a file, an FTP destination, or two tables of its own. The bot is the
only thing that knows all of that at once, which is why communities put it on their site.

JSON by default; `format: xml` writes the classic bot's own element and attribute names, so a status
page written for it keeps working. Uploads run on a worker thread, so a slow FTP server cannot stall
the bot, and credentials in the URL never reach the log. Two deliberate differences from the classic: a
masked admin is masked here too (this file is usually public, and that is what `!mask` is for), and
player IP addresses are left out unless `include_ip` asks for them. On shutdown the last write says the
server is empty — without it a page shows the players who were here when the bot stopped.
Config: `examples/plugin_status.yaml`.

### login — proving an admin is that admin

On most of these engines a player's identity is a guid their own client sends, and some titles hand out
none at all — so a spoofed identity is an admin account. With this plugin anybody above
`threshold_level` is a regular from the moment they connect until they type `!login <password>`, in the
game's private-message console rather than in chat. `!setpassword` sets your own, or that of somebody
**below** you: setting a password is taking an account.

Passwords are stored as a PBKDF2 hash with a per-password salt, where the classic stored a bare MD5.
An imported classic database keeps working — a 32-character digest is recognised, verified as a legacy
hash, and replaced with a modern one the first time its owner logs in — but the wider column needs
**migration 0003**, so run `b3 db upgrade` before enabling it on an existing database. Three wrong
passwords stop the guessing for a minute, which the classic did not do at all.
Config: `examples/plugin_login.yaml`.

### ipban — the ban a new guid does not escape

A ban is enforced on a guid, and a guid is evaded by reinstalling. This kicks anybody connecting from an
address that is behind an active ban or tempban — checked at authentication and again the moment an
address becomes known, since on Call of Duty and Quake 3 the log line that authenticates a player
carries no address. There is no list to maintain: the addresses come from the bans already issued, so
lifting a ban stops keeping that address out. Players above `max_level` (a registered user, by default)
are exempt, because a household address shared with somebody banned is the case this exists not to
punish. Config: `examples/plugin_ipban.yaml`.

### nickreg — the name that belongs to somebody

Impersonating an admin is the cheapest trick on a game server. `!registernick` reserves the name you
are using; anybody else wearing it is warned to change it, on a name change and on a sweep of the
connected players. `!listnick` shows what is reserved and `!deletenick` drops a registration — your own,
or anybody's with `manage_level`, and never one belonging to a player above you.

Names are compared as players see them: colour codes stripped, trimmed, lower-cased, so `^1Adm^7in` and
`admin` are the same nickname. Registrations live in the plugin's own table and outlive the session,
which is the point — the trick is played when the owner is away. Nothing is checked for a moment after a
map change, because a player still loading has whatever name the engine gave them.
Config: `examples/plugin_nickreg.yaml`.

### makeroom — a slot for the people who pay for the server

On a popular server the members are the ones who cannot get in. `!makeroom` (alias `!mkr`) announces
why, kicks the newest arrival from the lowest group, and holds the slot for `retain_free_duration`
seconds: a non-member who takes it in the meantime is kicked too, and a member arriving ends the window,
which is the whole point of it. `!makeroomauto on|off` (alias `!mrauto`) does the same continuously,
keeping `min_free_slots` free — off by default, because that setting is a door policy rather than
moderation. The slot count comes from the server (`sv_maxclients`); `total_slots` in the config is for
stating a smaller number than the truth, which is what slots reserved outside the bot look like.

It removes players who have done nothing wrong, so what it refuses matters more than what it does. Never
a bot (the engine refills it, so nothing is freed), never an admin wearing a mask (a mask hides rank, it
does not remove it), never the admin who asked, and — unless you set `only_when_full: no` — never on a
server that still has a free slot. "Newest" means the newest *arrival*, not the newest database record.
Config: `examples/plugin_makeroom.yaml`.

### callvote — the one power a player has over everybody

`callvote kick` on whoever is winning, `callvote map` to whatever empties the server. Each vote type
gets a level in the config, a vote from below it is cancelled with a private word to the player, and a
`maps:` table can set a level per map — which *replaces* the level for `map`, so you can lock the type
down and still leave the two maps that fill the server open to everybody. A type nobody has listed uses
`default_level`. `!lastvote` reports the last finished vote on this server, and `!veto` cancels the one
running.

Two things it will not do. Below `min_voters` humans nothing is checked at all, because a vote with one
voter has already passed by the time the log line reporting it is read — and bots are not voters. And
**cancelling a vote is engine-specific**: Urban Terror 4.2/4.3 have the verb, Homefront and Altitude
announce a vote and take no instruction about it. On those two the plugin says so once at startup and
then only records and announces, rather than telling a player they may not do what is about to happen
anyway.

**The `protect:` section stops players voting your admins off the server** — the vote protector from
the classic's `poweradminhf`, which is here rather than in a per-game plugin for the same reason
`censorurt` became a `mute:` section on `censor`: it is a policy about votes, and this is the plugin
that already holds the running vote, the level table and the veto. It works wherever the engine says
who a vote is *about*: Homefront names the player in the vote line, and on the Quake 3 engines the
vote's own argument (`kick bob`, `clientkick 3`) is looked up. Where a vote can be cancelled it is
cancelled outright — which the Homefront plugin could not do — and where it cannot, the caller is
warned and a **ban** that passes is lifted again; a kick leaves nothing to undo. The rule is the
classic's: the target has to reach the level *and* outrank the caller, so two admins of equal rank are
left to disagree. **It is off until you set `protect.level`**, because this plugin runs on every family
with votes while the plugin it comes from ran on one title, and an upgrade should not start punishing
players for votes an operator has allowed for years. Config: `examples/plugin_callvote.yaml`.

### spree — the server saying a player's name

Five kills without dying gets a line to the whole server, and whoever ends it gets the credit; seven
deaths in a row gets a word of encouragement. Both tables are the operator's, keyed by the count, with a
`start` and an `end` message and `{player}` / `{victim}` in them — a placeholder this plugin cannot fill
is refused when the config is read rather than sent to the server as written.

A streak is kills *without dying*, so every kind of death breaks it: shot, team-killed, or walked off a
ledge. Team-killing does not build one. A death nobody caused ends a spree in silence, because the `end`
message names who did it and there is nobody. Counts reset at the end of a map, or per round if you ask
for it, or never. `!spree [player]` reports the streak somebody is on.
Config: `examples/plugin_spree.yaml`.

### firstkill — first blood

Three announcements, once each per map: the first kill, the first kill *if it was a headshot*, and the
first player to shoot their own side. They go out centre-screen where the engine has a verb for that and
as ordinary chat where it has not. `!firstkill`, `!firsttk` and `!firsths` switch each on or off and
report the current state when given neither.

The headshot line needs the engine to have said where the shot landed: Call of Duty states a hit location
on every kill, Source sets a headshot flag, and Urban Terror states it on the `Hit:` line before the kill
(threaded onto the kill by the parser, since a UrT `Kill:` line names only the weapon). On the families
that report a weapon and nothing else it never fires, and nothing needs configuring for that.
Config: `examples/plugin_firstkill.yaml`.

### netblocker — a network nobody may connect from

For the nuisance that keeps returning on a new account from the same place. List the networks —
`10.0.0.0/8`, a single address, `1.2.3.5 - 1.2.3.9`, or IPv6 — and anybody connecting from one is
removed. `max_level` is what keeps it usable: a registered player is exempt by default, because
everybody behind a listed address is refused whether they have done anything or not.

Checked at authentication *and* when an address first becomes known, since on Call of Duty and Quake 3
the line that authenticates somebody carries no address at all. A bad entry in the list is refused at
startup by name and the rest still load. Config: `examples/plugin_netblocker.yaml`.

### duel — two players settling something

`!duel <player>` challenges somebody; they accept with `!duel <you>`. After that every kill between the
two is counted and both are told the score privately — nobody else sees any of it. `!duelreset` zeroes a
score, `!duelcancel` calls it off, and both take a name when several duels are running. The score is
also reported at the end of a round, and to the player left behind when their opponent leaves.

Every kind of kill between the two counts, **including a team kill**: two people on the same team is the
commonest duel there is, and the classic plugin watched ordinary kills only, so those duels stayed 0:0
forever. A challenge nobody answers expires (the classic's waited indefinitely), and one player may have
`max_duels` running at once, because each of them messages both players after every kill.
Config: `examples/plugin_duel.yaml`.

### spawnkill — shooting somebody who cannot shoot back yet

Two windows measured from the moment a player spawns: `hit` (shooting them) and `kill`, each with its own
delay, exempt level, penalty and reason. Every kind of shot counts, **team kills and team damage
included** — from the victim's side it makes no difference whose bullet it was, and the classic plugin
punished neither.

A player the bot has not seen spawn is never a victim, which is what makes a bot started mid-round safe.
The plugin needs the engine to report spawns (the Quake 3 family and Frostbite do); where it does not,
nothing is recorded and nobody is punished, with no game names anywhere in the plugin. `warn`, `kick` and
`tempban` are the penalties; the classic's `slap`, `nuke` and `kill` were game-server verbs this bot has
no seam for, and configuring one is refused at startup rather than silently doing nothing.
Config: `examples/plugin_spawnkill.yaml`.

### geolocation — where a player is connecting from

Answers no commands and says nothing in game: it resolves addresses and publishes the result for
`location`, `geowelcome` and `countryfilter` to use. It reads a **local MaxMind-format `.mmdb`** and makes
no network requests, so no player's address leaves the machine and there is no service to be down. The
classic plugin queried three web services before trying a local file, and two of those services have
since shut down.

You supply the database. **DB-IP "IP to Country Lite"** is a monthly `.mmdb` with no account and a CC-BY
licence — start there; **MaxMind GeoLite2** needs a free account and a licence key but adds city, region,
coordinates and (with GeoLite2-ASN) the network operator. Install the reader with `pip install b3ng[geo]`.
Place names are folded to ASCII by default, because a Quake 3 console cannot draw `Córdoba` and a row of
question marks is worse than "Cordoba". Config: `examples/plugin_geolocation.yaml`.

### location — where everybody is

The user-facing half of the geo family: it announces each arrival's place and adds `!locate`,
`!distance` (great-circle kilometres) and `!isp`. Load it after `geolocation`, which owns the database;
without one it announces nothing and answers "I do not know", which is the truthful reply rather than a
fault.

What it can say depends on the database. A country file (DB-IP Lite, GeoLite2-Country) answers `!locate`;
`!distance` needs coordinates, so a city file; `!isp` needs GeoLite2-ASN as the second database. Missing
fields produce the message that says so — the classic substituted them as `--`, so a country-level
database answered "Bob is connected from -- (Germany)". Nothing is announced for the first few minutes
after the bot starts, since a restart mid-match authenticates everybody at once.
Config: `examples/plugin_location.yaml`.

### countryfilter — which countries may play here

Apache's model, which is what the classic modelled it on: `deny,allow` (allowed unless denied — a
blocklist) or `allow,deny` (denied unless allowed — a whitelist), with `all` available in either list.
Exemptions by level, name and address are checked first, then an address blocklist, then the countries.

Two things worth knowing before switching it on. **Under `deny,allow` the allow list overrides the deny
list**, so `allow_from: all` — which the classic shipped as its default — makes any blocklist a no-op;
`allow_from` starts empty here and writing that combination logs a warning naming it. And a country the
database cannot name is in **neither** list, so it is refused by a whitelist and admitted by a
blocklist: the classic matched with `str.find`, and an empty string is found in anything, so those
players were refused by *any* deny list. `exempt_names` works but is spoofable — a name is whatever the
player typed. Config: `examples/plugin_countryfilter.yaml`.

### poweradminurt — Urban Terror's own commands

The classic plugin is 3,846 lines and forty-nine commands. What is here so far:

**Player control** — `!paslap` and `!panuke` (with a repeat count up to 25, one a second), `!pakill`,
`!pamute` / `!paunmute`. Each is an engine verb, so each asks whether this title has one and says so
plainly when it does not, rather than the plugin refusing to load by game name.

**Moving players about** — `!paforce <player> <red|blue|spec|free> [lock]` (the lock puts them back if
they switch, which is the only thing that makes it a lock), `!paswap` for two players changing places,
and `!paswapteams` / `!pashuffleteams` for everybody at once — those two name no player, which is what
`GameProfile.server_verbs` is for.

**The team balancer**, which is a policy rather than a command: it keeps the two sides the same size,
moving whoever joined theirs most recently, and puts a player back if their switch is what made the
teams uneven. `!pateams` asks for a balance now. It works the whole move out in one pass — the classic
moved one player, asked the server to count the teams again, and believed an answer that could not yet
include the move it had just made, so it picked the same player up to twenty-five times. Every
deliberate move of players (a shuffle, a swap, a `!paforce`, a round start, the bot adopting a server
full of players) opens a quiet window the automatic checks stand down for; that is the classic's
`ignoreSet`, and without it the balancer's next pass undoes the shuffle an admin just asked for.

**The skill balancer**, which is the same idea about a different quantity: two sides of four are even
in numbers and not in anything else. Each player is scored on what they have done *since they joined
the team they are on* — kill ratio, net contribution per minute, headshot ratio, and what they did for
the mission, which counts for more than the scoreboard — and a shuffle deals everybody out repeatedly
and keeps the evenest arrangement it finds. `!paskuffle` shuffles, `!pabalance` reaches the same place
by moving as few players as it can, `!paunskuffle` deliberately makes the sides unfair so the other two
can be watched working, `!paadvise` says which side is winning and how badly, and `!paautoskuffle`
chooses what happens automatically (`off`, `advise`, `balance`, `shuffle`). Where the arrangement
cannot be made any more even, the shuffle spreads the *snipers* instead — a rifle that kills in one
shot decides a game if they all end up on one side. A shuffle has to beat the teams being played: the
classic's search started from nothing, so the first deal always won and `!paskuffle` on even teams
moved half the server about and then reported the difference unchanged.

**The name checker** — two players may not wear one name, a handful of names nobody may wear at all
(the default nickname, and `all`, which is the word admin commands use to mean everybody), and a limit
on how often one player may rename during a map. It does not overlap `nickreg`, which answers "is this
a name registered to somebody else?" where this answers "is this a name at all?". Names are compared
as players see them — colours stripped, trimmed, lower-cased — where the classic compared raw strings,
so `^1Bob` and `^2Bob` were not duplicates of each other.

**Match mode** — `!pamatch on` tells the engine a match is being played, switches off the plugins an
operator named for it, and runs a config file. Everything automatic in this plugin stands down while it
is on, since those policies exist to keep a *public* server pleasant. Only the plugins it switched off
come back: the classic re-enabled its whole list, so one an operator had deliberately turned off
returned because a match had happened. Its config files are not looked for on the bot's own disk
either — the classic did that before sending `exec`, so with the bot on another machine a perfectly
good config was skipped in silence.

**The vote delay**, which stops players calling a vote for the first few minutes of a round. A deadline
rather than the classic's thread per round, none of which it cancelled: two rounds inside the delay
left two timers running and the first handed voting back mid-round.

**The rotation manager**, which switches the map list by how busy the server is, with a margin either
side of each switch point. The classic's hysteresis took a "was the last thing a join or a part?"
argument and had the join case backwards, so joining a player could *shrink* the maps; here the margin
is applied against the rotation being played, which is what hysteresis means. It also counts the
players who will be on the map rather than everybody connected — not the spectators, and not the bots
its own bot support added.

**The headshot counter**, which announces how many headshots a player has landed and tells newcomers
what the helmet and the kevlar are for. It needs an engine that reports where a shot landed — Urban
Terror, here — and asks it to log that, since without `g_loghits` there are no hits to count. The
classic's `broadcast: True`, its default, handed the announcement to `console.write`, which sends an
rcon *command*: a line of prose is not one, so on the setting most operators were running the
announcement went to the server and nowhere else.

**Bot support**, which keeps the server topped up with AI players on maps an operator has listed as
safe — the classic's config file warns in capitals that this may crash the server, and that list is the
mitigation, so an empty one means no map rather than every map. While it is off, nothing writes a bot
cvar at all; the classic rewrote the skill setting on every config load whether or not it was running
bots, and never wrote `bot_enable` back to 0.

**The spectator check**, which asks whoever has been watching rather than playing to do one or the
other once the server is busy enough that somebody wants the slot. It warns, and `admin`'s escalation
is what removes them — the number of warnings a kick takes is a server's policy, not this feature's.
Nothing happens while `g_maxGameClients` is set, because that means the server is deciding who plays
and putting the surplus in the spectators itself; the classic read that cvar once at startup, with no
error handling, so a server that answered nothing for it failed to load the plugin at all. A bot counts
towards "busy" — it fills a slot — and is never warned.

**The match settings**, which are a *table* rather than twenty-one near-identical methods: the gametype
switches (`!pactf`, `!pabomb`, `!pajump`, …), the on/off toggles (`!painstagib`, `!pahardcore`, …), the
bounded numbers (`!pasetgravity`, `!patimelimit`, the wave delays, …), `!pastamina`, `!pamoon`,
`!pasetnextmap`, and `!pagear` with its `+weapon`/`-weapon` bits.

**The server commands** — `!pabigtext`, `!paset` / `!paget`, `!pavote`, `!pamaprestart`, `!pamapreload`
(which is what applies a changed password), `!pacyclemap`, `!paexec <file>` — at level 80, since what a
config file contains is anything, and the name has to look like a filename rather than being sanitised
into one — and `!papublic on|off`, whose private password is built from a configured word plus fresh
digits, sent to the admin privately and never written to the log.

One immunity rule covers all four player commands — you cannot use them on somebody at or above your own
level. The classic had `slap_safe_level` on `!paslap`, a different comparison on `!pamute`, and *nothing*
on `!panuke`. `!paveto` is not here: `callvote`'s `!veto` is that command, and it works on any engine
that can cancel a vote. Config: `examples/plugin_poweradminurt.yaml`.

### poweradminbf3 — Battlefield 3's teams, and the two lists its server keeps

The largest of the classic's four Frostbite `poweradmin*` plugins and the one the other three are
near-copies of. Round control (`!roundnext`, `!roundrestart`, `!endround`, `!serverreboot` — which
asks first, since it restarts the game server). Players: `!kill` and `!nuke` kill without touching
the scoreboard, `!changeteam` moves one, and `!swap` exchanges two — the command that needs **squads**,
because a Battlefield squad is four players who spawn on each other, so a swap has to put each player
into the *other's* squad, which is three moves and not two. Keeping the sides even: `!autoassign` puts
an arriving player on the smaller team, `!autobalance` moves the most recent joiners when the teams
drift apart, and the **scrambler** deals everybody out again at a round or a map boundary — by the
previous round's score where the server reported one. Then the settings (`!unlockmode`, `!vehicles`,
`!idle`, `!gunmaster`), the four `!yell` commands, `!setnextmap`, and the two lists a Battlefield
server keeps: the reserved slots (`!vips`, `!vipadd`, …) and the preset server configs
(`!listconfig`, `!loadconfig`). Its `!punkbuster` has moved to the admin plugin, where every
PunkBuster family can reach it.

**Two of its features were dead.** The auto-scrambler's gamemode blacklist announced the skip with a
log line that raised `TypeError` — `r"…%s…" + " …" % blacklist` binds the `%` to the second literal,
which has no placeholder — and everything after it in the round-start handler was skipped, which is
the flag that lets autoassign run and the deadline the autobalancer waits on. So on any server whose
gamemode was on that list, **autoassign and autobalance were both dead for the whole map**. The
captured test covering it asserted that no scramble happened, which was true, and looked like the
feature working.

The rest is the same shape: `time.sleep` in four command handlers, 2.5 seconds in `!nuke` and up to
twenty inside the autobalancer, all on the bot's one thread; a config loader on
`thread.start_new_thread`; a score-based scramble that sorted the scores as *text*, so `"9"` outranked
`"100"` and the strategy meant to even out skill dealt almost at random; three different level checks
across four commands and **none at all on `!nuke`**; a swap that went ahead with one player at the
deploy screen and moved the other to team 0; a join-order list kept by name, so a rename left an entry
that never came out; and `!endround` calling `max()` on an empty dict. Its `serverconfigs` directory
lived inside the plugin's own installed source tree, so it is a setting here.

Writing it needed the squad and scoreboard plumbing deliberately left out of the Frostbite parser,
which turned up two more: this engine's centre-screen message (`admin.yell`) was never wired up, so
every `say_big` on all six Frostbite titles was an ordinary chat line, and `Console.set_cvar` sent
`set <name> "<value>"` where Frostbite takes the setting's name as the command — so every setting
written on this family was answered `UnknownCommand` in silence.
Config: `examples/plugin_poweradminbf3.yaml`.

### poweradminbfbc2 — Bad Company 2, and a team balancer that never balanced

The same shape as `poweradminbf3` a generation earlier, and most of the differences are the
generation: round control is spelled in levels rather than rounds, the map list is a flat list of
level names so the next map is an *index* into it, a game mode is chosen by **playlist** and takes
effect at the next map, and there is no spectator team — `!paspectate` puts a player back on the
deploy screen, which is the only thing the engine offers. `!pamatch on` is the other half: everybody
types `!ready`, whoever has not is nagged every ten seconds, and when they all have the bot counts
down from ten and restarts the map, with the plugins you list switched off for the duration.

**The team balancer had never moved anybody.** Three separate faults, each enough on its own.
`getTeams()` appended both teams to the same list, so team 2 was always empty and the gap was always
the size of team 1 — three-a-side read as six against nobody. `teambalance()` then read the join time
with `c.isvar(...)`, which returns a **bool**, and asked that bool for `.value`: `AttributeError`,
every time, on both the automatic path and `!pateams` — and it raised *after* putting "Autobalancing
Teams!" on everybody's screen, so the announcement was the only part that worked. And the sort was
ascending, so the players it would have moved are whoever had been on that team longest, which is the
opposite end of the list from whoever just made the sides uneven.

`!pachangeteam` compared a team id to the string `"1"` where the parser makes it an `int`, so it
moved *everybody* to team 2 — the players already there stayed put while the bot reported "forced to
the other team". **Four of the five yell commands did not yell**: `!payellteam`, `!payellenemy`,
`!payellsquad` and `!payellplayer` all sent an ordinary private chat line. `!pamaplist` built its
command as one string rather than a word list, so a filename that the R9 protocol does not take
anyway went out glued to the verb. `!pamapreload` emptied the server's map list, put one map in it,
cycled, and then re-read the list from disk — discarding the two commands before it, and any rotation
an admin had built. `!paset friendlyFire` with no value was an `IndexError`. And a match cost the
server its team balancer permanently: `!pamatch on` switched it off and nothing ever switched it back.

Writing it turned up three faults below the plugin. This title's rotate verb is
`admin.runNextLevel`, and both Frostbite 1 titles were being sent Medal of Honor's
`admin.runNextRound` — so `!map` inserted the map, pointed the server at it and then failed on the
one command that loads it. Its centre-screen message counts **milliseconds**, so the ten seconds the
profile asked for was ten milliseconds: every `say_big` on Bad Company 2 was a flash nobody could
see. And neither Frostbite 1 title had a single round or yell verb declared, which is what this
plugin was waiting on. Config: `examples/plugin_poweradminbfbc2.yaml`.

### poweradminmoh — Medal of Honor, and a move the server always refused

The twin of `poweradminbfbc2` — same engine generation, same ready-up match mode, a team balancer of
the same shape — with a **scrambler** the other has not got, `!swap`, and the list of reserved
spectator slots this title keeps. `!scramble` deals the teams out again when the round ends,
`!scramblemode score` uses the previous round's scoreboard to spread the strongest players across
both sides, and `!autoscramble round|map` runs it by itself.

**Every feature that moved a player was refused by the server.** This title's `admin.movePlayer`
takes **three** arguments where the rest of the family takes four, and the classic sent four — its own
changelog records fixing that as "a major fix … this affected all team balancing features", which is
exactly what it costs: `!changeteam`, `!swap`, `!spect`, the balancer and the scrambler all did
nothing, and the plugin swallowed the refusal so nothing said so. The verb is on the title's profile
here, where there is one place for it to be right.

The rest reads like a plugin nobody had opened in fifteen years. `!swap` with no arguments crashed,
because the guard was `if not input:` — the *builtin*, which is always truthy — so the check never ran
and the next line indexed `None`; and a swap with one player spectating went ahead and moved the other
to team 0, the guard being `and` where it meant `or`. The score scramble sorted the score *strings*, so
`"9"` outranked `"100"`. The scrambler dealt out spectators and the server's own bots. `!setnextmap`
for a map nobody has answered with the *letters* of what you typed, `", ".join(data)` over a string
being a list of characters. `!reserveslot` compared the server's refusal — a string — to a
one-element list, so "already has a slot" could never be said. `movedByBot` was set before the move
and never cleared when it failed, so the player's next genuine team change was ignored — and this is
the only plugin in the classic tree that reads that variable. A `matchmode_configs` section the code
looked for assigned into an attribute that does not exist, and `AttributeError` was not in the tuple
it caught, so an operator who wrote that section lost the whole plugin at load. And match mode cost
the server its team balancer permanently, exactly as in Bad Company 2.

Two facts about the title came out of it, both now on its profile. It has **no centre-screen
message** — the classic's own parser overrides `saybig` to call `say`, which is a parser author saying
there is no `admin.yell` here — so everything this plugin says is chat, and `say_big` falls back
rather than being answered `UnknownCommand`. And **team 3 is the spectators**, which is why the
balancer does not count the people watching as a side. Config:
`examples/plugin_poweradminmoh.yaml`.

### poweradminhf — Homefront, and a match manager for the wrong game

Filed with the Frostbite `poweradmin*` plugins for years — in this project's own notes as well as by
resemblance — and it is not one of them: it is Homefront. The resemblance is that the same author
copied the same match manager into it and changed nothing.

**That copy is the port's headline.** Its nags and its countdown send `('admin.yell', …)` and it
finishes with `('admin.restartMap',)` — Frostbite verbs, as *tuples*, to a parser whose `write` takes a
string. So `!pamatch on` announced a match, registered `!ready`, and then every nag, the whole
countdown and the restart at the end went nowhere. The ready-up here is the shared one, and it ends
with the only instruction this engine takes about its rotation: `admin nextmap`, so a match starts on
the next map rather than on this one again.

**Two commands were broken by an operator.** `!paautobalance off` sent the server the single word
`false` — `'admin SetAutoBalance %s' % 'true' if on else 'false'` binds the `%` before the conditional,
so turning it on worked and turning it off did not, and neither said anything, so nobody could tell.
`!pateambalance` with no argument answered the same way and so replied with the bare word `off`
whenever it was off. And the balancer's other half, the "you unbalanced the teams" warning, **could
never fire**: it tested a player's *name* against a list of Steam ids, because a 2011 change moved
every verb here to Steam ids and left the comparison behind. Beyond that: no level check anywhere,
so any moderator could `!pakill` a senior admin; `!paident` put a player's Steam id in public chat and
offered an IP address this engine never reports; and match mode cost the server its team check
permanently.

**The vote protector left the plugin.** Stopping players calling kick and ban votes against admins is
`callvote`'s `protect:` section now — it is a policy about votes, and that plugin already holds the
running vote, the level table and the veto. On Homefront it does what this plugin did; on an engine
that can cancel a vote it stops it outright, which this one could not.

What this engine can do to a player is on its profile (`kill`, `switch_team`, `spectate`, plus the
server's `autobalance` and `cyclemap`), which is what let the plugin be written without a Homefront
command inside it. One of those is worth knowing: **`switch_team` takes no team.**
`admin forceteamswitch` puts a player on the other side and cannot be asked for a particular one, so
the balancer moves players *off* the bigger side — the same thing on a two-sided game, and the only
thing offered. Config: `examples/plugin_poweradminhf.yaml`.

### urtserversidedemo — evidence an admin can collect

Urban Terror 4.2 can record a demo of one player from the server side, which is the only kind of
evidence about a suspected cheater that does not need that player's cooperation. `!startserverdemo
<player>` starts one, `!startserverdemo all` records everybody and keeps recording whoever joins next,
and `!stopserverdemo` reverses either. A length can be asked for — `!startserverdemo bob 5` — which in
the classic was reachable only from two third-party plugins, so for everybody else the machinery
existed and nothing could ask.

**The reply is the point.** `startserverdemo` answers with the filename it has begun writing, and the
bot logs it with the player's name, GUID and address; without reading that, nothing can say a demo
started or find the file again. `jumper` needs exactly that, which is why this is a service as well as
two commands. Reading a verb's reply needed a seam of its own: every engine verb until now was
fire-and-forget, so `Console.ask_verb` is `apply_verb` for the few whose answer is the result.

**Nothing on shutdown ever ran** in the classic. `onExit` and `onStop` were defined and never
registered as handlers, so a bot that stopped left the server recording — and `!startserverdemo all`
outlived the bot that asked for it. The method they would have called could not have worked anyway:
`for guid, stopper in self._auto_stop_timers:` iterates a dict, which yields keys, so unpacking a GUID
into two names raises. Two faults in one path, each hiding the other. Beyond that: a reply the author
had not anticipated hit `raise AssertionError` **inside a thread**, so the attempt vanished with the
thread and the player stayed in the waiting table for good; a timed demo of a player who had already
left raised `KeyError` instead of stopping; and the two commands disagreed about what `all` meant
(`!startserverdemo ALL` looked for a player called "ALL"). There are no threads and no timers here —
one scheduled pass, and the retry happens the moment the player joins a team, which is what the thread
was waiting for.

The verbs belong to the title: `record_demo`/`record_stop` and `record_all`/`record_stop_all` are
declared on 4.2 and 4.3 and **not on 4.1**, so the plugin asks the profile rather than the server and
switches itself off with a reason on the older build. Config:
`examples/plugin_urtserversidedemo.yaml`.

### poweradmincod7 — Black Ops, where you cannot just load a map

Call of Duty: Black Ops is the one engine here with no map verb worth the name. A **ranked** server
takes its map and its gametype from a *playlist*, and the only lever over which map comes up next is
`playlist_excludeMap` — the list of maps the playlist may not pick. So `!pasetmap nuketown` excludes
the other twenty-five maps and puts the operator's own exclusion list back when the round ends. The
window in between is real: if the bot is killed inside it the server keeps them excluded, which is why
the reply says so. `!paexcludemaps` sets that list directly, `!paplaylist` / `!pagetplaylists` /
`!pasetplaylist` work the playlists, `!pasetdlc 1 off` switches a map pack off (the command does
Treyarch's off-by-one for you), and on an **unranked** server `!pamaprestart`, `!pafastrestart` and
`!pagametype` do what they say. Each command says when the server will not obey it rather than
quietly doing nothing.

`!palistcfg` and `!paload` send a server `.cfg` file over rcon a line at a time, which needs a
`config_dir` naming where those files are — the classic used the bot's own config folder, which a
plugin cannot know.

**`!pagametype` never worked.** It sent `g_gametype tdm` as a bare assignment, and Black Ops takes a
dvar only through `setadmindvar` — the same fault this bot already records against that title's
`g_logsync` — so the gametype was unchanged and the map restarted anyway. Four other faults were in
the same shape as the rest of this exercise: `time.sleep` inside four command handlers (twenty-five
seconds of frozen bot to list the playlists, one chat line per second while it did);
`threading.Thread(target=self._configloader(data))`, which *calls* the loader and hands the thread its
return value, so a config file was sent from the command handler with a sleep per line and the admin
was told "successfully loaded" before anything had gone out; a restored exclusion list that could be
the literal word `None`; and two crashes on ordinary typing (`!paset g_gametype` with no value,
`!pasetplaylist 2.7`). Fixing the first of those also turned up a core bug: `Console.set_cvar` capped
every value at 128 characters, the length that suits a ban reason, so a twenty-five-map exclusion list
— or any real `sv_mapRotation` — was cut off mid-word and rejected by the engine in silence.

`!paversion` is not here (`!plugin info` answers it) and neither is `!paident`, which showed a
player's IP and guid at level 40 and would broadcast them with `@paident`; `!clientinfo` is that
command, at level 80 and private. Config: `examples/plugin_poweradmincod7.yaml`.

### codam — a Call of Duty admin mod's commands

CoDAM and the admin mods like it take their instructions through a single rcon verb, and this plugin
turns those verbs into bot commands: `!ckick bob spamming` reaches the mod as `command "kick 7
spamming"`. The bot cannot ask a mod what it understands, so an operator lists the verbs — the ones
that name a player and the ones that do not — and that list is the whole command surface. `!codam
<line>` sends a line exactly as typed, at superadmin, because that is every power the mod has.

Reading the classic version found that **the half of it that took no player never worked at all**:
those verbs were registered as `c` + the verb and then sent to the mod with the `c` still attached, so
CoDAM was asked for `crestart`. The other half stripped it, which is what shows the intent. Nothing
raised — a mod that does not recognise a command says so to the rcon caller, and the classic threw that
reply away, so an admin saw the same silence whether the command ran, was not recognised, or the mod
was not installed. It also put the player *after* the free text (`kick spamming 3`, which the mod reads
as a player called "spamming"), pasted an admin's typing into a quoted rcon argument unescaped, and let
`!ckick` reach a level the bot's own `!kick` refuses to touch. It shipped no config file and is named
nowhere in the classic's own distribution config, which is how all of that went unnoticed.
Config: `examples/plugin_codam.yaml`.

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
