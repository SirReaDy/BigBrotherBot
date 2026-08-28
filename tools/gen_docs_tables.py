"""Write the tables that are really data, from the code that holds the data.

Two tables in the documents are not prose: the supported-titles grid and the admin command
reference. Both already exist as facts in the source — `b3.parsers.games.by_family()` and the
commands `AdminPlugin` registers — and both were being maintained by hand in two files.

That does not hold. While this was being written, `README.md` said **"Ten engine families"** twice
while its own table listed thirteen rows and the code reported thirteen; `tools/check_counts.py`
guards the test and plugin counts but had no opinion about families, so nobody saw it. A number a
reader cannot check reads as a measurement, and this one had been wrong for months.

**Only the tables are generated.** The prose around them is the most valuable writing in `docs/`, and
no generator produces sentences like "a hit line carries no damage figure, so the damage comes from a
weapon×hit-location table". So this fills marked regions and leaves everything else alone::

    <!-- generated:titles -->
    ...this tool's output...
    <!-- /generated:titles -->

Run with ``--check`` to verify the committed files match without writing. CI does that, so a title
added to a profile table fails the build until the documents catch up — which is the moment to update
them, and the only moment anybody would remember to.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

#: A generated region, named so one file can hold more than one.
BLOCK_RE = re.compile(
    r"(?P<open><!-- generated:(?P<name>[a-z0-9_-]+) -->\n)"
    r"(?P<body>.*?)"
    r"(?P<close><!-- /generated:(?P=name) -->)",
    re.DOTALL,
)

#: What an operator has to *configure* for a family, in the same three words `b3 games` uses. Kept
#: identical on purpose: two descriptions of one fact is one description and one thing to go stale.
SOURCE_PUSH = "events over rcon"
SOURCE_FILE_RCON = "log + a command file"
SOURCE_LOG = "reads a game log"

#: Families whose parser is a *dialect* of another, and the title they extend. `b3 games` lists them
#: flat, which is right for "what may I write in the config?"; a reader of the table also wants to
#: know that Urban Terror is Quake 3 plus its own lines, because that is what decides which page of
#: `docs/games.md` applies to them.
DIALECT_OF = {
    "urt": "Quake 3",
    "et": "Quake 3",
    "sof2": "Quake 3",
    "wop15": "Quake 3",
}

#: Display names. The family id is what goes in a config; this is what a person calls it.
FAMILY_NAMES = {
    "altitude": "Altitude",
    "battleye": "BattlEye",
    "cod": "Call of Duty",
    "et": "Enemy Territory",
    "frontline": "Frontlines: Fuel of War",
    "frostbite": "Frostbite",
    "homefront": "Homefront",
    "q3": "Quake 3",
    "ravaged": "Ravaged",
    "sof2": "Soldier of Fortune 2",
    "source": "Source",
    "urt": "Urban Terror",
    "wop15": "World of Padman 1.5",
}


#: What a title is *called*, where the id does not say. The only thing on this page with no home in
#: the code, and it stays here rather than becoming a `GameProfile` field: the bot has no use for a
#: marketing name, and a field nothing reads is one more thing to be wrong. A title missing its gloss
#: still appears — losing an annotation is harmless where losing a title is not.
TITLE_NOTES = {
    "plutoiw5": "MW3",
    "plutot6": "Black Ops 2",
}


def titles_table() -> str:
    """The supported-titles grid, from the profile tables themselves."""
    from b3.parsers.games import ALIASES, FILE_RCON_FAMILIES, PUSH_FAMILIES, by_family

    accepted_as: dict[str, list[str]] = {}
    for old, current in ALIASES.items():
        accepted_as.setdefault(current, []).append(old)

    rows = ["| Family | How events arrive | `server.game` |", "|---|---|---|"]
    for family, titles in by_family().items():
        if family in PUSH_FAMILIES:
            source = SOURCE_PUSH
        elif family in FILE_RCON_FAMILIES:
            source = SOURCE_FILE_RCON
        else:
            source = SOURCE_LOG
        name = FAMILY_NAMES.get(family, family)
        parent = DIALECT_OF.get(family)
        label = f"**{name}**" + (f"<br><small>{parent} + its own lines</small>" if parent else "")
        listed = []
        for title in titles:
            entry = f"`{title}`"
            note = TITLE_NOTES.get(title)
            # The classic bot's id for a renamed title still works, and that is worth saying where
            # somebody is migrating a config: `q3a` is `q3` here.
            also = accepted_as.get(title)
            if also:
                note = f"also {', '.join(f'`{a}`' for a in sorted(also))}"
            if note:
                entry += f" <small>({note})</small>"
            listed.append(entry)
        rows.append(f"| {label} | {source} | {' '.join(listed)} |")
    return "\n".join(rows)


def commands_table() -> str:
    """The admin command reference, read off the declarations instead of kept beside them.

    `@command(level=…, alias=…)` already carries every column, and the docstring is written as
    ``name <args> - what it does`` — which is the row. So this is a transcription that was being
    done by hand, in a file 215 lines long, for sixty-two commands whose levels an operator is
    trusting. Sorted by level and then by name, the order the page already used: a reader asking
    "what can a level-20 admin do?" wants them together.
    """
    from b3.plugins.admin import AdminPlugin

    rows = ["| Command | Alias | Level | What it does |", "|---|---|---|---|"]
    specs = []
    for attribute in dir(AdminPlugin):
        meta = getattr(getattr(AdminPlugin, attribute, None), "_command", None)
        if meta is not None:
            specs.append(meta)
    for meta in sorted(specs, key=lambda m: (m["level"], m["name"])):
        first = (meta["help"] or "").splitlines()[0] if meta["help"] else ""
        # `aliases <player> - list the other names a player has used`. The usage half carries the
        # arguments, which is the part a reader needs and the command name alone does not give.
        usage, _, summary = first.partition(" - ")
        usage = usage.strip() or meta["name"]
        summary = summary.strip() or "—"
        alias = f"`!{meta['alias']}`" if meta["alias"] else ""
        # A pipe ends a cell, backticks or not — and `!plugin <list|enable|disable|info>` has three
        # of them, which would silently split one row into five columns.
        usage = usage.replace("|", r"\|")
        rows.append(
            f"| `!{usage}` | {alias} | {meta['level']} | {summary[0].upper() + summary[1:]} |"
        )
    return "\n".join(rows)


#: Which generated block goes in which file. A name appearing in no file is a typo, and is reported.
BLOCKS = {
    "titles": titles_table,
    "commands": commands_table,
}


def apply(text: str, produced: dict[str, str], seen: set[str]) -> str:
    def replace(match: "re.Match[str]") -> str:
        name = match["name"]
        seen.add(name)
        if name not in produced:
            raise SystemExit(f"unknown generated block {name!r}; known: {sorted(produced)}")
        return match["open"] + produced[name] + "\n" + match["close"]

    return BLOCK_RE.sub(replace, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if any generated block is stale, and write nothing",
    )
    args = parser.parse_args(argv)

    produced = {name: build() for name, build in BLOCKS.items()}
    targets = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]

    seen: set[str] = set()
    stale: list[Path] = []
    for path in targets:
        if not path.is_file():
            continue
        current = path.read_text(encoding="utf-8")
        wanted = apply(current, produced, seen)
        if current == wanted:
            continue
        if args.check:
            stale.append(path)
        else:
            path.write_text(wanted, encoding="utf-8")
            print(f"updated {path.relative_to(ROOT).as_posix()}")

    missing = sorted(set(produced) - seen)
    if missing:
        print(
            f"generated block(s) {missing} are produced but appear in no document — "
            "either add the markers or drop the generator",
            file=sys.stderr,
        )
        return 1

    if stale:
        for path in stale:
            print(f"{path.relative_to(ROOT).as_posix()}: generated table is stale", file=sys.stderr)
        print("Run: python tools/gen_docs_tables.py", file=sys.stderr)
        return 1
    print(f"{len(seen)} generated block(s) up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
