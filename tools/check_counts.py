"""Check the numbers the documents state against the repository they describe.

`tools/check_links.py` exists because a wrong link is silent. A wrong *number* is worse: it reads as
a measurement. "2,861 tests" and "34 bundled plugins" are claims a reader has no way to check, they
are repeated in three files, and every one of them drifts the moment something lands — this project
has already corrected a test count that was 1,983 against an actual 2,525 and a source figure that was
22,981 against 33,775.

So the figures are measured here and compared with what each document says.

Four kinds of claim, and they are treated differently on purpose:

* **Counts** — tests, test files, bundled plugins — must match **exactly**. They change when somebody
  adds a file, which is a moment to update the sentence that counts them.
* **Lines of code** are allowed to drift by `LOC_TOLERANCE`, because they change with every commit and
  a document that has to be edited on every commit gets edited carelessly instead. Past the tolerance
  the figure has stopped being true rather than gone slightly stale.

`PARITY.md` and `TODO.md` are gitignored planning documents, so in CI only `README.md` and `docs/` are
present — the check reports what it found rather than insisting a file exists. Run it directly
(`python tools/check_counts.py`), and it exits non-zero naming every disagreement with the measured
value, so the output is the fix list.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: How far a lines-of-code figure may be from the truth before it counts as wrong. Five per cent of
#: forty thousand lines is two thousand, which is a fortnight's work rather than a commit's.
LOC_TOLERANCE = 0.05

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Claim:
    """One number a document states, and what it is a number of."""

    path: Path
    line: int
    label: str
    stated: int
    measured: int
    exact: bool

    def wrong(self) -> bool:
        if self.stated == self.measured:
            return False
        if self.exact:
            return True
        allowed = max(1.0, self.measured * LOC_TOLERANCE)
        return abs(self.stated - self.measured) > allowed

    def describe(self) -> str:
        drift = "" if self.exact else f" (tolerance ±{LOC_TOLERANCE:.0%})"
        return (
            f"{self.path.relative_to(ROOT).as_posix()}:{self.line}: {self.label} says "
            f"{self.stated:,} but the repository has {self.measured:,}{drift}"
        )


def python_lines(directory: Path) -> int:
    """Lines in every `.py` file under a directory, counted the way `wc -l` counts them."""
    total = 0
    for path in sorted(directory.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        total += len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return total


def test_files() -> int:
    return len([p for p in (ROOT / "tests").glob("test_*.py")])


def bundled_plugins() -> list[str]:
    """Plugin packages that ship with the bot, which is what "bundled" means in the documents."""
    plugins = ROOT / "src" / "b3" / "plugins"
    return sorted(
        item.name
        for item in plugins.iterdir()
        if item.is_dir() and item.name != "__pycache__" and (item / "__init__.py").is_file()
    )


def collected_tests() -> int | None:
    """How many tests pytest collects. None when it cannot be asked.

    Collection rather than a run: it takes a second, it needs no database and no sockets, and the
    number it reports is the one the documents quote.
    """
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=300,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"could not ask pytest how many tests there are: {exc}", file=sys.stderr)
        return None
    match = re.search(r"(\d+)\s+tests?\s+collected", out)
    return int(match.group(1)) if match else None


def as_int(text: str) -> int:
    return int(text.replace(",", "").replace("_", ""))


def claims(measured: dict[str, int]) -> list[Claim]:
    """Every number this repository's documents state about itself.

    Each pattern is anchored on the sentence that carries it rather than on a bare number, because a
    document is full of numbers that are not measurements — the classic bot's own figures, a port's
    line count, a level, a year.
    """
    found: list[Claim] = []
    patterns: list[tuple[str, str, str, bool]] = [
        # README's "where it stands today" paragraph.
        ("README.md", r"([\d,]+) tests, `mypy --strict` clean", "tests", True),
        # PARITY's scorecard rows.
        (
            "PARITY.md",
            r"\|\s*Source LOC\s*\|[^|]*\|\s*([\d,]+) \(\+[\d,]+ tests, \+[\d,]+ fake servers\)",
            "source lines",
            False,
        ),
        (
            "PARITY.md",
            r"\|\s*Source LOC\s*\|[^|]*\|\s*[\d,]+ \(\+([\d,]+) tests",
            "test lines",
            False,
        ),
        (
            "PARITY.md",
            r"\|\s*Source LOC\s*\|[^|]*\|\s*[\d,]+ \(\+[\d,]+ tests, \+([\d,]+) fake servers\)",
            "fake-server lines",
            False,
        ),
        (
            "PARITY.md",
            r"\|\s*Test files\s*\|[^|]*\|\s*([\d,]+) \([\d,]+ tests\)",
            "test files",
            True,
        ),
        ("PARITY.md", r"\|\s*Test files\s*\|[^|]*\|\s*[\d,]+ \(([\d,]+) tests\)", "tests", True),
        (
            "PARITY.md",
            r"\|\s*✅\s*\*\*Ported and bundled\*\*\s*\|\s*([\d,]+)\s*\|",
            "bundled plugins",
            True,
        ),
        # TODO's own count section.
        ("TODO.md", r"\*\*([\d,]+) bundled here\*\*", "bundled plugins", True),
    ]
    labels = {
        "tests": "tests",
        "test files": "test files",
        "source lines": "source lines",
        "test lines": "test lines",
        "fake-server lines": "fake-server lines",
        "bundled plugins": "bundled plugins",
    }
    for filename, pattern, label, exact in patterns:
        path = ROOT / filename
        if not path.is_file():
            continue  # a gitignored planning document, absent in CI
        expected = measured.get(labels[label])
        if expected is None:
            continue  # nothing to compare against, e.g. pytest could not be asked
        compiled = re.compile(pattern)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = compiled.search(line)
            if match is None:
                continue
            found.append(
                Claim(
                    path=path,
                    line=number,
                    label=label,
                    stated=as_int(match.group(1)),
                    measured=expected,
                    exact=exact,
                )
            )
    return found


def main() -> int:
    measured: dict[str, int] = {
        "source lines": python_lines(ROOT / "src"),
        "test lines": python_lines(ROOT / "tests"),
        "fake-server lines": python_lines(ROOT / "tools" / "fakeservers"),
        "test files": test_files(),
        "bundled plugins": len(bundled_plugins()),
    }
    counted = collected_tests()
    if counted is not None:
        measured["tests"] = counted

    stated = claims(measured)
    if not stated:
        print("no counted claims found in the documents; has a sentence been reworded?")
        return 1

    wrong = [claim for claim in stated if claim.wrong()]
    for claim in wrong:
        print(claim.describe())

    print()
    for label, value in measured.items():
        print(f"{label:>18}: {value:,}")
    if wrong:
        print(f"\n{len(wrong)} stale figure(s) across {len({c.path for c in stated})} document(s)")
        return 1
    print(f"\n{len(stated)} stated figure(s) agree with the repository")
    return 0


if __name__ == "__main__":
    sys.exit(main())
