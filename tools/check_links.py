"""Check every relative link and anchor in the repository's markdown.

The reason this exists: the README was one file, so every cross-reference in it was an anchor into
itself and could not go stale. Splitting it into `docs/` turned those into links *between* files, and
a wrong one is **silent** -- GitHub renders it as an ordinary link and scrolls nowhere when clicked.
That is the same failure mode as a log pattern matching nothing, which is the class of bug this
project keeps finding, so it gets a check rather than a convention.

Only tracked files are examined, via `git ls-files`, so the gitignored planning documents are not
checked here: they are not published, and they link to each other freely.

Run it directly (`python tools/check_links.py`), or let the CI workflow do it. Exits non-zero and
names every bad link, with the file and line, so the output is the fix list.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

#: A markdown inline link's target: `[text](target)`. Reference-style links and bare autolinks are
#: not used in this repository; if that changes, this pattern has to grow.
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

#: An ATX heading. Setext headings (underlined with `===`) are not used here.
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

#: Targets that are not paths into this repository.
EXTERNAL = ("http://", "https://", "mailto:", "tel:")


def slug(heading: str) -> str:
    """GitHub's anchor for a heading text.

    Lowercase, drop anything that is not a word character, space or hyphen, then spaces to hyphens.
    Inline code fences and emphasis markers vanish because their characters are dropped.
    """
    text = heading.replace("`", "")
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return text.strip().lower().replace(" ", "-")


def anchors(text: str) -> set[str]:
    """Every anchor a markdown file offers, including the `-1`, `-2` suffixes for repeats."""
    found: set[str] = set()
    seen: dict[str, int] = {}
    fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        match = HEADING_RE.match(line)
        if match is None:
            continue
        base = slug(match.group(2))
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    return found


def tracked_markdown() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"], capture_output=True, check=True, text=True
    ).stdout
    return [Path(line) for line in out.splitlines() if line]


def main() -> int:
    files = tracked_markdown()
    if not files:
        print("no tracked markdown files found; is this a git checkout?", file=sys.stderr)
        return 1

    cache: dict[Path, set[str]] = {}
    problems: list[str] = []

    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for target in LINK_RE.findall(line):
                if target.startswith(EXTERNAL) or target.startswith("#") and len(target) == 1:
                    continue
                relative, _, fragment = target.partition("#")
                where = path if not relative else (path.parent / relative)
                if relative:
                    if not where.exists():
                        problems.append(f"{path}:{number}: no such file: {target}")
                        continue
                    if where.suffix.lower() != ".md":
                        continue  # a link to a script or a config; existence is the whole check
                resolved = where.resolve()
                if fragment:
                    if resolved not in cache:
                        cache[resolved] = anchors(resolved.read_text(encoding="utf-8"))
                    if fragment not in cache[resolved]:
                        problems.append(
                            f"{path}:{number}: no heading '{fragment}' in {where}: {target}"
                        )

    for problem in problems:
        print(problem)
    checked = len(files)
    if problems:
        print(f"\n{len(problems)} bad link(s) across {checked} markdown file(s)")
        return 1
    print(f"all links and anchors resolve, across {checked} markdown file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
