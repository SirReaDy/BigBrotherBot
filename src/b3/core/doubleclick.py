"""What `b3.exe` does when somebody double-clicks it.

The Windows build is for the operator who has never installed Python, and that operator starts a
program by double-clicking it. A console program given no command prints its usage and exits, so
what they saw was a window that flashed and vanished - the first, and for many the last, thing the
bot ever did for them.

So when Windows opened the console *for this program alone* and it was given no arguments, it
behaves like an application instead:

* with no server set up yet, it asks the `b3 init` questions (`b3.core.wizard`, the same ones -
  there is one list of questions, not two) and offers to start the bot;
* with one already set up, it offers to start it, or to set up another;
* and whatever happens, including an error, the window waits for Enter before it closes, because a
  message nobody had time to read is no message.

Started from cmd or PowerShell, or with any argument at all, none of this applies and `b3.exe` is
exactly the `b3` command.

**Where the servers live**: `%USERPROFILE%\\b3\\<name>`, not beside the exe. The release is a folder
that an upgrade replaces whole, and a config kept inside it would be deleted with the old version.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from pathlib import Path

from b3.core.wizard import Prompter

#: Where a double-clicked bot keeps its servers, one folder each.
SERVERS_DIR = Path("~/b3")

#: What a server's name may be. It becomes a folder name, so nothing a path would trip over.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _console_processes() -> int:
    """How many processes share this console - 0 where there is no Windows console to ask."""
    if sys.platform != "win32":
        return 0
    import ctypes

    ids = (ctypes.c_uint32 * 16)()
    return int(ctypes.windll.kernel32.GetConsoleProcessList(ids, len(ids)))


def _own_processes() -> int:
    """How many of the console's processes are this program.

    A one-file PyInstaller build is two: a small bootloader that unpacks the bundle and waits, and
    the Python inside it. Every other way of running this is one.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is None:
        return 1
    # The folder build runs in place, from `_internal` beside the exe; the one-file build runs from
    # wherever it unpacked itself, which is somewhere else.
    return 1 if Path(meipass).resolve().parent == Path(sys.executable).resolve().parent else 2


def started_by_double_click(
    argv: list[str],
    *,
    console_processes: Callable[[], int] = _console_processes,
) -> bool:
    """Whether Windows opened this console for this program, with nothing on its command line.

    Run from a shell, the shell is attached to the console as well, so there are more processes on
    it than this program's own. That is the whole test - and it is why an argument is checked first:
    `b3.exe --version` is a command, however it was started.
    """
    if argv:
        return False
    # Git Bash's terminal is not a Windows console, so a program started from it is handed a hidden
    # console of its own - which counts like a double-click. What it is not handed is a console for
    # input and output: those are pipes there, and a real console on a double-click.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    count = console_processes()
    return 0 < count <= _own_processes()


def bot_command() -> list[str]:
    """The command that starts the bot itself: the exe, or this Python running `b3.cli`."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "b3.cli"]


def servers(home: Path | None = None) -> list[Path]:
    """Every server set up under `~/b3`, as the path to its `b3.yaml`, by name."""
    root = (home or SERVERS_DIR).expanduser()
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob("*/b3.yaml") if path.is_file())


def _free_name(root: Path) -> str:
    """`server1`, or the first `serverN` that is not taken."""
    number = 1
    while (root / f"server{number}").exists():
        number += 1
    return f"server{number}"


def _valid_name(root: Path) -> Callable[[str], str | None]:
    def check(answer: str) -> str | None:
        if not NAME_RE.match(answer):
            return "letters, digits, '-', '_' and '.', starting with a letter or digit"
        if (root / answer / "b3.yaml").exists():
            return f"{root / answer} is already set up; pick another name"
        return None

    return check


def set_up(ask: Prompter, home: Path | None = None) -> Path | None:
    """Ask the questions and write the server. Returns its `b3.yaml`, or None if nothing was made."""
    from b3.cli import _create
    from b3.core import wizard
    from b3.core.instance import InstanceSpec

    root = (home or SERVERS_DIR).expanduser()
    ask.say(f"Each game server gets its own folder under {root}.")
    name = ask.ask("A short name for this server", _free_name(root), validate=_valid_name(root))
    directory = root / name
    answers = wizard.ask(
        directory, ask, defaults=InstanceSpec(directory=directory, name=name), ask_name=False
    )
    # The flags `b3 init` would have had: none, so its defaults. The geolocation download is kept -
    # a first start is exactly when a current database is worth the few seconds.
    flags = argparse.Namespace(service=False, service_user="b3", force=False, no_geoip=False)
    ask.say()
    if _create(flags, answers.spec, run_doctor=answers.run_doctor) != 0:
        # `doctor` failing is not the same as nothing having been written: the server is set up,
        # and what is wrong with it has just been printed.
        config = directory / "b3.yaml"
        return config if config.is_file() else None
    return directory / "b3.yaml"


def choose(ask: Prompter, found: list[Path]) -> Path | None:
    """Which server to start: the one there is, one of several, or None to set up a new one."""
    if len(found) == 1:
        name = found[0].parent.name
        if ask.confirm(f"Start the bot for {name} ({found[0].parent})?", True):
            return found[0]
        return None
    ask.say("Servers set up on this machine:")
    for number, config in enumerate(found, start=1):
        ask.say(f"  {number}. {config.parent.name}  ({config.parent})")
    ask.say("  n. set up a new one")

    def valid(answer: str) -> str | None:
        if answer.lower() == "n" or (answer.isdigit() and 1 <= int(answer) <= len(found)):
            return None
        return f"a number from 1 to {len(found)}, or n"

    answer = ask.ask("Which one", "1", validate=valid)
    return None if answer.lower() == "n" else found[int(answer) - 1]


def start(config: Path, ask: Prompter, supervise: Callable[[list[str]], int] | None = None) -> int:
    """Run the bot for `config` in this window, restarting it when `!restart` asks."""
    from b3.cli import _supervise

    ask.say()
    ask.say(
        f"Starting the bot for {config.parent.name}. Close this window or press Ctrl+C to stop."
    )
    ask.say()
    return (supervise or _supervise)([*bot_command(), "-c", str(config), "run"])


def run(ask: Prompter | None = None, home: Path | None = None) -> int:
    """The whole double-click session. Always waits for Enter before the window can close."""
    import logging
    import traceback

    prompter = ask or Prompter()
    from b3.core.style import configure_logging

    configure_logging(logging.INFO, "%(asctime)s %(levelname)s %(name)s: %(message)s")
    status = 0
    try:
        from b3 import __version__

        prompter.say(f"B3 {__version__} - Big Brother Bot")
        prompter.say()
        found = servers(home)
        config = choose(prompter, found) if found else None
        if config is None:
            config = set_up(prompter, home)
            if config is not None and not prompter.confirm("Start the bot now?", True):
                prompter.say(f"Double-click b3.exe again to start it, or run: b3 -c {config} run")
                config = None
        if config is not None:
            status = start(config, prompter)
    except (EOFError, KeyboardInterrupt):
        prompter.say()
        prompter.say("stopped")
    except Exception:  # noqa: BLE001 - shown, then waited on: a window that vanishes says nothing
        traceback.print_exc()
        status = 1
    prompter.say()
    try:
        prompter.ask("Press Enter to close this window")
    except (EOFError, KeyboardInterrupt):
        pass
    return status


__all__ = [
    "NAME_RE",
    "SERVERS_DIR",
    "bot_command",
    "choose",
    "run",
    "servers",
    "set_up",
    "start",
    "started_by_double_click",
]
