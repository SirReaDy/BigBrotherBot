"""Convert a classic B3 configuration into this one's, and say what it could not convert.

`b3 import-db` brings a classic install's *data* across. This brings its *settings* — the numbers
somebody spent years tuning, spread over `b3.xml` and a `plugin_*.ini` per plugin. The classic ships
43 of those files and 2,991 lines of them, so "just retype it" is an afternoon and a transposed digit
nobody notices for a month.

**It converts what is provably safe and refuses to guess at the rest.** That refusal is the point,
not a limitation. A converter that copied every line across would produce a file that looks complete
and is quietly wrong in the few places where the same words mean something different, and a
plausible-looking config is worse than an obviously incomplete one — you stop looking at it.

Four kinds of thing it will not convert, each reported with what to do instead:

* **A setting the plugin no longer has.** Checked against the plugin's own ``DEFAULTS``, not against
  a table kept here, so this stays true as plugins change. A key that is not in ``DEFAULTS`` was
  renamed, restructured or dropped, and only a person can say which.
* **A section that moved somewhere else entirely.** `[commands]` set a plugin's command levels in the
  classic; here that is the `cmdmanager` plugin, deliberately, so that overrides do not live in other
  plugins' files. `[messages]` is the `messages:` block of `b3.yaml`.
* **A plugin that is not a plugin any more.** Seven became core services, three became a section on a
  plugin we already had, and four are gone. Their files have no destination and saying so is more
  use than writing one nothing will read.
* **A plugin whose config file is gone, though the plugin is not.** `cmdmanager` keeps what it is
  told in its own tables now, so a converted file would be one nothing reads.

Two things it *does* convert are worth naming, because the obvious implementation drops both:

* **A section that was folded into ``settings``.** Several plugins grew from a block per feature —
  `[teambalancer]`, `[speccheck]` — into one `settings:` with the feature in the key name. Asking
  ``DEFAULTS`` is what tells a fold apart from a rename, and it is the authority `[settings]` is
  already checked against, so no second table ages here. Reporting these as unmappable was wrong
  twice over: the keys are unchanged, and a file that converts nothing reads as a plugin that cannot
  be migrated when in fact there was nothing left to do.
* **The operator's own writing under a name that changed.** `[killingspree_messages]` is
  `killing_sprees:`, and `[guest commands]` is `commands: {guest: ...}` — the level left the section
  name and became a key. Nothing derives these, so they are listed; the list stays short because a
  section earns an entry only when the move is a pure rename. Where the *placeholders* changed with
  the name, they are translated: a `%player%` carried across is not a placeholder here, it is the
  literal text the server would print.

What it does *not* do is produce a config you can run unread. `tk`'s `levels` is the clearest case:
the classic named which groups get penalised, here each level has its own kill/damage/ban
multipliers. No tool derives the second from the first.
"""

from __future__ import annotations

import configparser
import importlib
import importlib.util
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: `b3.xml` -> `b3.yaml`. Small enough to write out, unlike the plugin settings, and the only place
#: a hand-kept mapping is the right tool: these keys were renamed rather than merely moved, and
#: several have no destination at all.
MAIN_SETTINGS: dict[tuple[str, str], str | None] = {
    ("b3", "parser"): "server.game",
    ("b3", "database"): "bot.database",
    ("b3", "bot_name"): "bot.name",
    ("b3", "bot_prefix"): "bot.prefix",
    ("b3", "time_zone"): "bot.time_zone",
    ("b3", "log_level"): "bot.log_level",
    ("b3", "logfile"): None,
    ("b3", "time_format"): None,
    ("server", "rcon_password"): "server.rcon_password",
    ("server", "port"): "server.port",
    ("server", "game_log"): "server.game_log",
    ("server", "rcon_ip"): "server.host",
    ("server", "public_ip"): "server.host",
    ("server", "punkbuster"): "server.punkbuster",
    ("server", "delay"): None,
    ("server", "lines_per_second"): None,
    ("plugins", "external_dir"): "bot.plugins_dir",
}

#: Why a `b3.xml` setting has nowhere to go. Printed rather than left as a blank, because "not
#: converted" and "you no longer need this" are very different things to read during a migration.
MAIN_GONE: dict[str, str] = {
    "logfile": "logs go to stdout now; your service manager decides where they land",
    "time_format": "timestamps are ISO-8601 and not configurable",
    "delay": "there is no read loop to pace - the bot is event-driven",
    "lines_per_second": "same: nothing is throttling a log read any more",
}

#: Plugins that are not plugins here, and what took over. Anything absent is an ordinary bundled
#: plugin whose settings are converted normally.
PLUGIN_FATE: dict[str, str] = {
    "ftpytail": "core: put the ftp:// URL in `server.game_log`",
    "sftpytail": "core: put the sftp:// URL in `server.game_log` (needs the `sftp` extra)",
    "httpytail": "core: put the http:// URL in `server.game_log`",
    "cod7http": "core: put the URL in `server.game_log`",
    "scheduler": "core: cron is a service plugins register with",
    "pluginmanager": "core: `b3 plugin` on the command line, `!plugin` in the game",
    "punkbuster": "core: enabled per title, with `server.punkbuster` to override",
    "geowelcome": "folded into `welcome` as two message variants",
    "censorurt": "folded into `censor` as its `mute:` section",
    "radio_spam_protection": "folded into `spamcontrol` as its `radio:` section",
    "publist": "dropped: it called a bigbrotherbot.net service that is long gone",
    "adv": "dropped: its news feed was the bigbrotherbot.net forum's RSS",
    "translator": "dropped on purpose - see README.md",
    "xlrstats": "not ported: it has its own web front end and belongs on the web API",
    "webapi": "planned as a core layer rather than a plugin",
}

#: `b3.xml` settings that moved to a *different* key rather than being renamed, with the sentence to
#: print. Separated from `MAIN_GONE` because "this is elsewhere" and "you no longer need this" send
#: an operator to two different places.
MAIN_MOVED: dict[str, str] = {
    "channel": (
        "update checking is `bot.update_check` (true/false) and `bot.update_remote` (the repository "
        "to check, empty to switch it off) - there are no release channels"
    ),
    "type": "the command reference is generated from the code now; see docs/commands.md",
    "maxlevel": "same: `autodoc` has no counterpart, the reference lists every command",
    "destination": "same: `autodoc` has no counterpart",
}

#: Settings from the classic `plugin_admin.ini` that are *core* config here rather than the admin
#: plugin's. Named individually because "this plugin has no such setting" is true and unhelpful:
#: they all still exist, one level up.
ADMIN_TO_CORE: dict[str, str] = {
    "hidecmd_level": "`bot.silent_level` in b3.yaml - the / prefix is core, not the admin plugin's",
    "command_prefix": "the four prefixes are fixed: ! normal, @ loud, & big, / silent",
    "command_prefix_loud": "fixed as @; `bot.loud_level` sets who may use it",
    "command_prefix_big": "fixed as &; `bot.loud_level` sets who may use it",
    "command_prefix_private": "fixed as /; `bot.silent_level` sets who may use it",
}

#: Sections whose keys are the **operator's own words**, not a schema: their rules, their reason
#: keywords, their custom commands. Copied wholesale, because there is nothing to check them
#: against — an entry called `rule7` is neither known nor unknown, it is theirs — and because these
#: are precisely the lines somebody wrote themselves and would most notice the loss of.
#:
#: Listed rather than guessed at. The distinction is real and not derivable: `[warn]` and
#: `[spamages]` are both key/value blocks in the same file, and one is a fixed set of settings while
#: the other is free text. Getting it the wrong way round would either discard an operator's rules
#: or silently accept a misspelt setting.
FREEFORM_SECTIONS: set[tuple[str, str]] = {
    ("admin", "spamages"),
    ("admin", "warn_reasons"),
    ("censor", "badwords"),
    ("censor", "badnames"),
    ("customcommands", "commands"),
    ("customcommands", "help"),
}

#: Sections that exist in a classic plugin config and belong somewhere else entirely here. Checked
#: *after* `FREEFORM_SECTIONS`, because two plugins own a `[commands]` section of their own: to
#: `cmdmanager` and `customcommands` it is their content, not a level override that moved away.
SECTION_MOVED: dict[str, str] = {
    "commands": (
        "command levels are the `cmdmanager` plugin's now, not each plugin's own file - "
        "so that an override lives in one place instead of in whichever plugin owns the command"
    ),
    "messages": "message overrides are the `messages:` block of `b3.yaml`",
}

#: Plugins that still exist here but take **no config file**, with what replaced the file. Distinct
#: from `PLUGIN_FATE`, where the plugin itself is gone: here the plugin is alive and it is the
#: settings that have nowhere to live, so writing the YAML would produce a file nothing reads —
#: which is the one outcome this tool exists to avoid.
PLUGIN_NO_CONFIG: dict[str, str] = {
    "cmdmanager": (
        "`cmdmanager` has no config file: a command's level and alias are set at runtime with "
        "`!cmdlevel` and `!cmdalias`, and kept in the plugin's own tables so they survive a "
        "restart. `update_config_file` has no counterpart either - nothing rewrites another "
        "plugin's file any more, so the file you wrote stays the file you wrote"
    ),
}

#: A classic section holding the **operator's own writing** under a name that changed. Nothing here
#: is derivable: `DEFAULTS` describes `settings:` and says nothing about a table of spree messages,
#: and the example config gives the new name but cannot say which old name it replaced.
#:
#: Kept short on purpose. A section only earns an entry when its content is the operator's and the
#: move is a pure rename — same shape, same meaning. A section whose *keys* were also renamed is
#: reported instead, because then only a person can do it.
SECTION_RENAMED: dict[tuple[str, str], str] = {
    ("spree", "killingspree_messages"): "killing_sprees",
    ("spree", "loosingspree_messages"): "losing_sprees",
}

#: The classic wrote a spree message with `%player%` and `%victim%`; this reads `{player}` and
#: `{victim}`. An exact pair, so translating is safe — and necessary, since a `%player%` carried
#: across is not a placeholder here, it is the literal text the server would print.
SPREE_PLACEHOLDERS = {"%player%": "{player}", "%victim%": "{victim}"}

#: `[guest commands]`, `[admin commands]` and their six siblings became one `commands:` block keyed
#: by level. The classic put the level in the section name; here it is a key, so that a command
#: defined for two levels is written once.
LEVEL_COMMANDS = re.compile(r"^(?P<level>\w+) commands$")


@dataclass
class FileResult:
    """What became of one classic config file."""

    source: Path
    target: Path | None = None
    converted: dict[str, object] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    skipped: str = ""


@dataclass
class ConvertReport:
    files: list[FileResult] = field(default_factory=list)

    @property
    def written(self) -> int:
        return sum(1 for f in self.files if f.target is not None)

    @property
    def attention(self) -> int:
        return sum(len(f.notes) for f in self.files)

    def render(self) -> str:
        out: list[str] = []
        for result in self.files:
            if result.skipped:
                out.append(f"{result.source.name}\n  ! SKIPPED: {result.skipped}")
                continue
            head = f"{result.source.name} -> {result.target.name}"  # type: ignore[union-attr]
            out.append(f"{head}\n  {len(result.converted)} setting(s) converted")
            out += [f"  ! {note}" for note in result.notes]
        out.append("")
        out.append(
            f"{self.written} file(s) written, {self.attention} item(s) need your attention."
            if self.attention
            else f"{self.written} file(s) written, nothing left over."
        )
        return "\n".join(out)


#: Where the annotated example configs live. They are the closest thing this project has to a
#: machine-readable schema for a plugin's *sections*: `DEFAULTS` describes `settings:` and nothing
#: else, and a classic `[warn]` or `[warn_reasons]` block has a destination `DEFAULTS` cannot see.
EXAMPLES = Path(__file__).resolve().parents[3] / "examples"


def _plugin_schema(name: str) -> dict[str, set[str]] | None:
    """The sections a plugin's config has, and the keys in each, read from its example file.

    Derived rather than declared, for the same reason `_plugin_defaults` asks the plugin: a table
    kept here would be a second description of the config format, and the second description is
    always the one that goes stale.

    This is what catches the renames that matter. The classic's `[warn]` block and ours share four
    keys and disagree about four more - `alert_kick_num` is `alert_at`, `instant_kick_num` is
    `kick_at`, `tempban_num` is `tempban_at`, `warn_delay` is `delay` - so a section copied wholesale
    would write four keys the plugin ignores while leaving its real ones at their defaults. Checked
    key by key, the four that match convert and the four that do not are reported.
    """
    path = EXAMPLES / f"plugin_{name}.yaml"
    if not path.is_file():
        return None
    try:
        import yaml

        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a malformed example must not stop somebody's migration
        return None
    if not isinstance(loaded, dict):
        return None
    return {section: set(body) for section, body in loaded.items() if isinstance(body, dict)}


def _is_bundled(name: str) -> bool:
    """Is there a plugin by this name here at all?

    Asked separately from `_plugin_defaults`, which returns ``None`` both for a plugin that is not
    here and for one that declares no settings. Those are different sentences to read mid-migration,
    and `cmdmanager`'s example config is pure documentation - it parses to nothing - so no amount of
    reading the examples can tell them apart.
    """
    try:
        return importlib.util.find_spec(f"b3.plugins.{name}") is not None
    except (ImportError, ValueError):
        return False


def _plugin_defaults(name: str) -> dict[str, object] | None:
    """The settings a bundled plugin actually accepts, read from the plugin itself.

    Asking the plugin rather than keeping a table here is what makes this stay true: a setting
    renamed next year is reported by the next run, with no maintenance. ``None`` when the plugin has
    no ``DEFAULTS`` — three do not — in which case keys are passed through unchecked and said so.
    """
    try:
        module = importlib.import_module(f"b3.plugins.{name}")
    except ImportError:
        return None
    defaults = getattr(module, "DEFAULTS", None) or getattr(module, "DEFAULT_SETTINGS", None)
    return dict(defaults) if isinstance(defaults, dict) else None


def _coerce(raw: str, default: object) -> object:
    """Read a classic value in the shape its 2.0 default has.

    An INI holds only strings. Writing `max_points: "400"` into YAML would work - the plugins read
    defensively — but it produces a config that looks hand-written by somebody who did not know the
    type, and the point of this is a file the operator will go on editing.
    """
    text = raw.strip()
    if isinstance(default, bool):
        return text.lower() in ("yes", "true", "on", "1")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(float(text))
        except ValueError:
            return text
    if isinstance(default, float):
        try:
            return float(text)
        except ValueError:
            return text
    if isinstance(default, list):
        return [item.strip() for item in text.split(",") if item.strip()]
    return text


def _read_classic(path: Path) -> dict[str, dict[str, str]]:
    """Read a classic plugin config, which may be `.ini` or the later `.xml` shape."""
    if path.suffix.lower() == ".xml":
        sections: dict[str, dict[str, str]] = {}
        root = ET.parse(path).getroot()
        for settings in root.iter("settings"):
            name = (settings.get("name") or "").strip().lower()
            entries = {
                (item.get("name") or "").strip(): (item.text or "").strip()
                for item in settings.iter("set")
            }
            sections.setdefault(name, {}).update(entries)
        return sections
    parser = configparser.ConfigParser(
        strict=False, interpolation=None, comment_prefixes=("#", ";")
    )
    parser.read(path, encoding="utf-8")
    return {name.lower(): dict(parser[name]) for name in parser.sections()}


def _yaml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]"
    text = str(value)
    # Quote anything YAML would read as something other than a string — a bare `yes`, `no`, `on` and
    # `off` are booleans there, and a level or a colour code is not.
    if text == "" or text.lower() in ("yes", "no", "on", "off", "true", "false", "null", "~"):
        return f'"{text}"'
    if re.search(r'[:#\[\]{}",]|^\s|\s$|^[&*!|>%@`]', text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _flatten(converted: dict[str, dict[str, object]]) -> dict[str, object]:
    """Every converted leaf as `section.key`, counting through one level of nesting.

    `customcommands` is the only nested one - `commands: {guest: {...}}` - and its commands are
    exactly what an operator wants counted, so the tally has to reach them.
    """
    flat: dict[str, object] = {}
    for section, body in converted.items():
        for key, value in body.items():
            if isinstance(value, dict):
                flat.update({f"{section}.{key}.{k}": v for k, v in value.items()})
            else:
                flat[f"{section}.{key}"] = value
    return flat


def convert_plugin(path: Path, out_dir: Path, *, write: bool = True) -> FileResult:
    """Convert one classic `plugin_<name>.ini`/`.xml` into `plugin_<name>.yaml`."""
    name = re.sub(r"^plugin_", "", path.stem).lower()
    result = FileResult(source=path)

    fate = PLUGIN_FATE.get(name)
    if fate is not None:
        result.skipped = fate
        return result

    no_config = PLUGIN_NO_CONFIG.get(name)
    if no_config is not None:
        result.skipped = no_config
        return result

    defaults = _plugin_defaults(name)
    schema = _plugin_schema(name)
    if defaults is None:
        result.notes.append(
            f"`{name}` declares no settings to check against - keys are copied unchecked, so "
            f"compare them against `examples/plugin_{name}.yaml`"
            if _is_bundled(name)
            else f"`{name}` is not a plugin bundled here - keys are copied unchecked, so compare "
            "them against the plugin's own documentation"
        )

    sections = _read_classic(path)
    converted: dict[str, dict[str, object]] = {}
    for section, entries in sections.items():
        # The operator's own writing is checked first: `cmdmanager` and `customcommands` each own a
        # `[commands]` section, and reporting *those* as "command levels live in cmdmanager now"
        # would send the one plugin that is cmdmanager looking for itself.
        freeform = (name, section) in FREEFORM_SECTIONS
        level_commands = LEVEL_COMMANDS.match(section) if name == "customcommands" else None
        renamed = SECTION_RENAMED.get((name, section))

        if not freeform and level_commands is None and renamed is None:
            moved = SECTION_MOVED.get(section)
            if moved is not None and entries:
                result.notes.append(f"[{section}] ({len(entries)} entries) not converted - {moved}")
                continue

        # `settings` is checked against the plugin's own DEFAULTS, which is authoritative for it;
        # every other section against the example config, which is the only description of its shape.
        known: set[str] | None
        dest = renamed or section
        if freeform or level_commands is not None or renamed is not None:
            known = None  # the operator's own entries: copy them, check nothing
        elif section == "settings" and defaults is not None:
            known = set(defaults)
        elif schema is not None:
            known = schema.get(section)
            if known is None:
                # A section that is gone may be a section that was *flattened*: several plugins
                # grew from one `[teambalancer]`-style block per feature into one `settings:` with
                # the feature in the key name. Asking DEFAULTS is what tells the two apart, and it
                # is the same authority `settings` is checked against - no second table to age.
                if defaults is not None and any(key in defaults for key in entries):
                    result.notes.append(
                        f"[{section}] folded into `settings` - `{name}` keeps every feature's "
                        "settings in one block now"
                    )
                    dest, known = "settings", set(defaults)
                else:
                    result.notes.append(
                        f"[{section}] not converted - `{name}` has no section by that name now; "
                        f"see `examples/plugin_{name}.yaml`"
                    )
                    continue
        else:
            known = None  # nothing to check against: pass it through, and the caller has said so

        for key, raw in entries.items():
            if known is not None and key not in known:
                core = ADMIN_TO_CORE.get(key) if name == "admin" else None
                result.notes.append(
                    f"[{section}] {key}: {raw!r} - {core}"
                    if core
                    else f"[{section}] {key}: {raw!r} - no key by that name now (renamed, "
                    f"restructured or gone); see `examples/plugin_{name}.yaml`"
                )
                continue
            hint = defaults.get(key, "") if (dest == "settings" and defaults) else ""
            value = _coerce(raw, hint)
            if renamed is not None and name == "spree" and isinstance(value, str):
                for classic, current in SPREE_PLACEHOLDERS.items():
                    value = value.replace(classic, current)
            if level_commands is not None:
                # `[guest commands]` -> `commands: {guest: {...}}`, the level a key rather than
                # part of a section name.
                level = level_commands.group("level")
                body = converted.setdefault("commands", {}).setdefault(level, {})
                body[key] = value  # type: ignore[index]
            else:
                converted.setdefault(dest, {})[key] = value

    result.converted = _flatten(converted)
    target = out_dir / f"plugin_{name}.yaml"
    if write:
        lines = [
            f"# Converted from {path.name} by `b3 import-config`.",
            "# Check it against examples/ before trusting it: anything this tool could not convert",
            "# was reported on the command line rather than guessed at.",
        ]
        for section, body in converted.items():
            lines.append(f"{section}:")
            for key, value in body.items():
                if isinstance(value, dict):
                    lines.append(f"  {key}:")
                    lines += [f"    {k}: {_yaml_value(v)}" for k, v in value.items()]
                else:
                    lines.append(f"  {key}: {_yaml_value(value)}")
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result.target = target
    return result


def convert_main(path: Path, out_dir: Path, *, write: bool = True) -> FileResult:
    """Convert `b3.xml` into `b3.yaml`."""
    result = FileResult(source=path)
    sections = _read_classic(path)
    values: dict[str, object] = {}
    plugins: list[str] = []

    messages: dict[str, str] = {}
    for section, entries in sections.items():
        if section == "plugins":
            plugins = [key for key in entries if key not in ("external_dir",)]
        if section == "messages":
            # The one section that converts wholesale: same keys, same `$variable` placeholders.
            # Worth doing rather than reporting, because these are the lines an operator wrote
            # themselves and would most notice the loss of.
            messages = {key: raw.strip() for key, raw in entries.items()}
            continue
        for key, raw in entries.items():
            mapped = MAIN_SETTINGS.get((section, key))
            if mapped is None:
                if key in MAIN_GONE:
                    result.notes.append(f"{section}/{key} not converted - {MAIN_GONE[key]}")
                elif key in MAIN_MOVED:
                    result.notes.append(f"{section}/{key} not converted - {MAIN_MOVED[key]}")
                elif (section, key) not in MAIN_SETTINGS:
                    result.notes.append(
                        f"{section}/{key} not converted - no equivalent is known; "
                        "see docs/migrating.md"
                    )
                continue
            values[mapped] = raw.strip()

    # `on`/`off` is how the classic wrote a boolean; YAML would read the string "on" as a string
    # here, and `server.punkbuster` is typed.
    punkbuster = values.get("server.punkbuster")
    if isinstance(punkbuster, str):
        values["server.punkbuster"] = punkbuster.strip().lower() in ("on", "yes", "true", "1")

    # The classic's log level was a number on its own scale; ours is a logging level name.
    raw_log = values.get("bot.log_level")
    if isinstance(raw_log, str) and raw_log.strip().isdigit():
        number = int(raw_log.strip())
        values["bot.log_level"] = "DEBUG" if number <= 10 else "INFO" if number <= 21 else "WARNING"
        result.notes.append(
            f"bot.log_level: the classic wrote {number} on its own scale; this reads a name - "
            f"wrote {values['bot.log_level']}, and DEBUG/INFO/WARNING/ERROR are the choices"
        )

    # A time zone abbreviation is not a zone. `CST` is US Central *and* China Standard, six hours
    # apart, so this is exactly the value not to guess at: every timestamp the bot writes depends
    # on it, and being silently wrong is worse than being told to fix it.
    zone = values.get("bot.time_zone")
    if isinstance(zone, str) and zone and "/" not in zone and zone.upper() != "UTC":
        result.notes.append(
            f"bot.time_zone: {zone!r} is an abbreviation, and several of them mean more than one "
            "zone (CST is US Central and China Standard). This needs an IANA name - "
            "`Europe/Berlin`, `America/Chicago` - and is left as you wrote it, so `b3 doctor` "
            "will tell you if it is not one"
        )

    if "server.game" in values:
        # The one value worth checking rather than copying: an unknown title now refuses to start,
        # and finding that out here beats finding it out at 3am.
        from b3.parsers.games import ALIASES, PROFILES

        raw_game = str(values["server.game"]).strip().lower()
        game = ALIASES.get(raw_game, raw_game)
        if game not in PROFILES:
            result.notes.append(
                f"server.game: {values['server.game']!r} is not a title this bot reads - "
                "`b3 games` lists all of them, and docs/migrating.md has the three that changed"
            )
        else:
            values["server.game"] = game

    database = values.get("bot.database")
    if isinstance(database, str) and database.startswith("mysql://"):
        result.notes.append(
            "bot.database: a MySQL URL needs its driver for SQLAlchemy - "
            "`mysql+pymysql://...`, with `pip install b3ng[mysql]`. Rewritten for you; check it"
        )
        values["bot.database"] = "mysql+pymysql://" + database[len("mysql://") :]

    result.converted = {**values, **{f"messages.{k}": v for k, v in messages.items()}}
    target = out_dir / "b3.yaml"
    if write:
        lines = [
            f"# Converted from {path.name} by `b3 import-config`.",
            "# `b3 doctor` checks this file, the database, the log and the rcon connection.",
        ]
        for group in ("bot", "server"):
            keys = {k: v for k, v in values.items() if k.startswith(f"{group}.")}
            if not keys:
                continue
            lines.append(f"{group}:")
            lines += [f"  {k.split('.', 1)[1]}: {_yaml_value(v)}" for k, v in keys.items()]
        if messages:
            lines.append("messages:")
            lines += [f"  {key}: {_yaml_value(value)}" for key, value in messages.items()]
        lines.append("plugins:")
        listed = [p for p in plugins if p not in PLUGIN_FATE] or ["admin"]
        for plugin in listed:
            lines.append(f"  - name: {plugin}")
            lines.append(f'    config: "@conf/plugin_{plugin}.yaml"')
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result.target = target
    return result


def convert_config_tree(source: Path, out_dir: Path, *, write: bool = True) -> ConvertReport:
    """Convert every classic config file in ``source`` into ``out_dir``."""
    report = ConvertReport()
    out_dir.mkdir(parents=True, exist_ok=True)

    for candidate in ("b3.xml", "b3.distribution.xml"):
        main = source / candidate
        if main.is_file():
            report.files.append(convert_main(main, out_dir, write=write))
            break

    for path in sorted(source.glob("plugin_*.*")):
        if path.suffix.lower() not in (".ini", ".xml"):
            continue
        report.files.append(convert_plugin(path, out_dir, write=write))

    log.info("config conversion complete: %d file(s) written", report.written)
    return report
