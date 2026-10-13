"""`b3 init --interactive` — the questions, in the operator's order rather than the schema's.

`b3 init` already writes a valid, commented config for any of the 38 titles, validates what it wrote
and quotes Windows paths correctly. What was missing is the **asking**: it takes flags, so an operator
has to know which flags exist before the command can help them — which is backwards for the one
command a new user runs first.

Two rules hold this together.

* **Every answer is validated as it is given.** The game against the profile table, the port as a
  port, the log as something that exists (or a URL), the database by *opening* it. An error at
  question three is worth ten times the same error after the file is written, when it arrives as a
  traceback from a command the operator did not connect to their typo.
* **It reads the schema; it does not restate it.** The defaults come from `InstanceSpec` and the game
  list from `b3.parsers.games.PROFILES`, so a title added tomorrow is offered by this without anybody
  remembering to add it. A generator with its own copy of the field list is a lie waiting to happen.

`ask()` takes its input and output as arguments, so the whole flow is testable without a terminal —
which is also what lets the same questions be driven from somewhere else later.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from b3.core.instance import InstanceSpec

#: What a plugin is called. Only used to refuse a typo before it reaches the config.
PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Offered when the operator does not name their own. Not "all of them": every one of these does
#: something on any title, and a new operator turning on twelve plugins at once cannot tell which of
#: them is responsible for what.
SUGGESTED_PLUGINS = ("admin", "censor", "spamcontrol", "tk", "stats", "welcome")

#: A log that is neither a path on this machine nor one of these is a typo, not a remote log.
URL_SCHEMES = ("ftp://", "ftps://", "sftp://", "http://", "https://", "file://")


@dataclass(frozen=True, slots=True)
class Answers:
    """What the wizard collected: an instance to create, and what to do afterwards."""

    spec: InstanceSpec
    run_doctor: bool = True


class Prompter:
    """Asks and prints. A class so a test can drive it with a list of answers."""

    def __init__(
        self,
        reader: Callable[[str], str] | None = None,
        writer: Callable[[str], None] | None = None,
    ) -> None:
        # Resolved when used, not captured here: a default argument binds `builtins.input` at import
        # time, so anything replacing it later — a test, an embedding caller — would be ignored.
        self._read = reader
        self._write = writer

    def _reader(self) -> Callable[[str], str]:
        return self._read if self._read is not None else input

    def _writer(self) -> Callable[[str], None]:
        return self._write if self._write is not None else print

    def say(self, text: str = "") -> None:
        self._writer()(text)

    def ask(
        self,
        question: str,
        default: str = "",
        *,
        validate: Callable[[str], str | None] | None = None,
    ) -> str:
        """Ask until the answer is usable. The default is shown and is what an empty answer means.

        `validate` returns a complaint, or None when the answer is good — a complaint rather than a
        boolean so the operator is told *what is wrong with this answer*, which is the whole
        difference between a form and a conversation.
        """
        while True:
            shown = f"{question} [{default}]: " if default else f"{question}: "
            answer = self._reader()(shown).strip() or default
            if validate is None:
                return answer
            complaint = validate(answer)
            if complaint is None:
                return answer
            self.say(f"  {complaint}")

    def confirm(self, question: str, default: bool = True) -> bool:
        suffix = "Y/n" if default else "y/N"
        answer = self._reader()(f"{question} [{suffix}]: ").strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes")


#: What lists the games, answered to the game question. Offered in the question itself rather than
#: pointing at `b3 games`: a double-clicked `b3.exe` asks this in a window with no shell to type it.
LIST_GAMES = "?"


def valid_game(answer: str) -> str | None:
    from b3.parsers.games import PROFILES, listing, suggest

    if answer in PROFILES:
        return None
    if answer == LIST_GAMES:
        # The listing *is* the complaint: the question is asked again underneath it.
        return "\n  ".join(listing())
    near = suggest(answer)
    if near:
        return f"no such game. Did you mean {' or '.join(near)}? Type {LIST_GAMES} to see them all"
    return f"no such game. Type {LIST_GAMES} to see all {len(PROFILES)} of them"


def valid_port(answer: str) -> str | None:
    if answer.isdigit() and 1 <= int(answer) <= 65535:
        return None
    return "a port is a number from 1 to 65535"


def valid_log(answer: str) -> str | None:
    """A game log is the bot's only source of events, so a typo here is the commonest first failure.

    A remote URL is accepted without being opened — the credentials in it are the operator's and this
    is not the moment to make them wait on a network round trip — but a *path* is checked, because a
    path that is not there is almost always a mistake this can catch now.
    """
    if not answer:
        return "the bot has no events without it"
    if answer.startswith(URL_SCHEMES):
        return None
    path = Path(answer).expanduser()
    if path.exists():
        return None
    return (
        f"{path} does not exist. Give the path on this machine, or a URL "
        "(ftp://, sftp://, http://) if the bot is not on the game server"
    )


def valid_database(answer: str, directory: Path | None = None) -> str | None:
    """Checked by *opening* it, which is the only way to tell a URL from a working database.

    A missing driver is the usual answer here — `mysql+pymysql://` with no PyMySQL installed — and it
    is much better heard now than at the first start.

    A relative SQLite path is checked **where the bot will use it**: in `directory`, the server's own
    folder, the way `b3.config.loader.resolve_sqlite_url` anchors it at startup. Opened as written,
    it was resolved against wherever `b3 init` was run instead - which tested a file the bot would
    never read, and left an empty `b3.sqlite` behind there (beside the exe, on a double-click).

    And a SQLite file that is not there yet is **not created** to prove it could be: the folder it
    would go in is checked instead, and the bot creates the file on first use. Connecting would have
    made it, and the server's folder does not exist yet at this point in the questions anyway.
    """
    if not answer:
        return "a SQLAlchemy URL, e.g. sqlite:///b3.sqlite"
    url = answer
    if directory is not None:
        from b3.config.loader import resolve_sqlite_url

        url = resolve_sqlite_url(answer, directory)
    try:
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url

        parsed = make_url(url)
        if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
            path = Path(parsed.database)
            if not path.exists():
                return _sqlite_folder_complaint(path)
        engine = create_engine(url)
        with engine.connect() as connection:
            # Reads the file's header on SQLite, where connecting alone does not: a file that exists
            # but is not a database is refused here rather than at the first start.
            connection.execute(
                text(
                    "SELECT 1" if parsed.get_backend_name() != "sqlite" else "PRAGMA schema_version"
                )
            )
        engine.dispose()
    except Exception as exc:  # noqa: BLE001 - the complaint is the whole point
        return f"could not open it: {exc}"
    return None


def _sqlite_folder_complaint(path: Path) -> str | None:
    """Whether a SQLite file could be created at `path`, without creating it - None if it could."""
    import os

    folder = path.parent
    # The folder may not exist yet - the server's own is made only once the questions are done - so
    # what has to be writable is the nearest part of the path that does.
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    if not folder.is_dir():
        return f"could not open it: {folder} is not a folder"
    if not os.access(folder, os.W_OK):
        return f"could not open it: {folder} cannot be written to"
    return None


def valid_plugins(answer: str) -> str | None:
    """Refuse a typo before it reaches the config. A name nobody bundles is *not* a typo, though —
    `b3 plugin install` puts third-party plugins under names this cannot know."""
    names = [name for name in re.split(r"[\s,]+", answer) if name]
    bad = [name for name in names if not PLUGIN_NAME_RE.match(name)]
    if bad:
        return f"not a plugin name: {', '.join(bad)}"
    return None


def bundled_plugins() -> list[str]:
    """Every plugin that ships with the bot, read from the package rather than from a list here.

    The same reading `b3.core.completion` does for the shell, and deliberately the same function:
    two answers to "what is bundled?" would eventually be two different answers.
    """
    from b3.core.completion import bundled

    return bundled() or list(SUGGESTED_PLUGINS)


def ask(
    directory: Path,
    prompter: Prompter | None = None,
    defaults: InstanceSpec | None = None,
    *,
    ask_name: bool = True,
) -> Answers:
    """Walk an operator through one game server. Returns what to create.

    The order is theirs, not the schema's: which game, where it is, how to talk to it, where its log
    is, where to keep the data, what to run.

    `ask_name=False` takes the name from `defaults`, for a caller that has already asked it - the
    double-clicked `b3.exe` asks for the name first, because the name is what picks the folder.
    """
    ask_it = prompter or Prompter()
    base = defaults or InstanceSpec(directory=directory)

    ask_it.say(f"Setting up a bot for one game server, in {directory}.")
    ask_it.say("Press enter to take the default in brackets.")
    ask_it.say()

    name = (
        ask_it.ask("A name for this instance (appears in logs)", base.name or directory.name)
        if ask_name
        else base.name or directory.name
    )
    game = ask_it.ask(
        f"Which game (type {LIST_GAMES} to list them)", base.game, validate=valid_game
    )
    host = ask_it.ask("Game server address", base.host)
    port = ask_it.ask("Game server RCON port", str(base.port), validate=valid_port)

    from b3.core.instance import _needs_command_file

    rcon_password = base.rcon_password
    command_file = base.command_file
    if _needs_command_file(game):
        # This family has no RCON password at all: the bot writes commands to a file the server
        # reads. Asking for a password here would be asking for something that does not exist.
        ask_it.say("  (this game has no RCON password — the bot writes commands to a file)")
        command_file = ask_it.ask("Command file the server reads", base.command_file)
    else:
        rcon_password = ask_it.ask("RCON password", base.rcon_password)
        if not rcon_password:
            ask_it.say("  (left empty — the bot can read the game but not act on it)")

    game_log = ask_it.ask(
        "Game log (a path here, or an ftp/sftp/http URL)", base.game_log, validate=valid_log
    )
    # The URL forms, said once: nobody types `mysql+pymysql://` from memory, and the default gives no
    # hint that anything but SQLite is on offer.
    ask_it.say(
        "  Database: the default is a SQLite file in this server's folder, which needs nothing set up."
    )
    ask_it.say(
        "  MySQL: mysql+pymysql://user:password@host/b3"
        "   PostgreSQL: postgresql+psycopg://user:password@host/b3"
    )
    database = ask_it.ask(
        "Database URL",
        base.database,
        validate=lambda answer: valid_database(answer, directory),
    )

    ask_it.say()
    ask_it.say(f"Plugins to start with. Available: {', '.join(bundled_plugins())}")
    chosen = ask_it.ask(
        "Which ones (space separated)", " ".join(SUGGESTED_PLUGINS), validate=valid_plugins
    )
    # `admin` first and once, whatever was typed: it is every other plugin's dependency, and an
    # operator who leaves it out has not asked for a bot without commands.
    named = [name for name in re.split(r"[\s,]+", chosen.strip()) if name]
    plugins = tuple(dict.fromkeys(["admin", *named]))

    run_doctor = ask_it.confirm("Check the server answers when this is written?", True)

    return Answers(
        spec=replace(
            base,
            directory=directory,
            name=name,
            game=game,
            host=host,
            port=int(port),
            rcon_password=rcon_password,
            game_log=game_log,
            database=database,
            command_file=command_file,
            plugins=plugins,
        ),
        run_doctor=run_doctor,
    )


def unknown_plugins(names: Sequence[str]) -> list[str]:
    """Which of these are not bundled. Not an error: a git-installed plugin is named the same way."""
    known = set(bundled_plugins())
    return [name for name in names if name not in known]


__all__ = [
    "SUGGESTED_PLUGINS",
    "LIST_GAMES",
    "Answers",
    "Prompter",
    "ask",
    "bundled_plugins",
    "unknown_plugins",
    "valid_database",
    "valid_game",
    "valid_log",
    "valid_plugins",
    "valid_port",
]
