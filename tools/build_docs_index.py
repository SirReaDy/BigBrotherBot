"""Build ``docs/index.md`` from ``README.md``, rewriting the links that only work on GitHub.

The site's home page and the repository's front page say the same things, and there is exactly one
place to change them: `README.md`. This copies it into the documentation tree at build time rather
than asking anybody to keep two files in step — a docs site maintained *alongside* the README drifts,
and then the wrong one is authoritative.

**The rewrite is the whole reason this is a program and not a symlink.** README links to its sibling
pages as ``docs/cli.md``, because that is where they are when you read it on GitHub. Inside the site
the same page *is* the docs root, so those links have to become ``cli.md`` — left alone they resolve
to ``docs/docs/cli.md`` and every link on the home page is dead. `mkdocs build --strict` does catch
that, which is what makes this safe: if the rewrite ever misses a shape, the build fails rather than
publishing a broken page.

Run with ``--check`` to verify the committed file is current without writing it. That is what CI
does, so an edit to `README.md` that nobody regenerated fails the build with the command to run.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
INDEX = ROOT / "docs" / "index.md"

#: Said in the file itself, because the first thing anybody does with a generated file is edit it.
BANNER = (
    "<!-- Generated from README.md by tools/build_docs_index.py. Do not edit: your change would be\n"
    "     overwritten by the next build, and CI fails when this file and the README disagree.\n"
    "     Edit README.md instead, then run `python tools/build_docs_index.py`. -->\n\n"
)

#: ``[CLI](docs/cli.md)`` -> ``[CLI](cli.md)``. Anchored on the opening paren so that prose
#: mentioning the path — "see docs/cli.md" — is left alone: only an actual link target moves.
DOCS_LINK_RE = re.compile(r"\]\(docs/([A-Za-z0-9_.-]+\.md)")

#: The same move for an asset referenced from raw HTML: ``<img src="docs/assets/doctor.svg">``. A
#: separate pattern because it is a different syntax, not because it is a different idea — and it is
#: here because the terminal demo on the home page is exactly that, and a rewrite that handled only
#: markdown links would publish a broken image without failing anything.
DOCS_SRC_RE = re.compile(r'(src|href)="docs/')


def render(readme: str) -> str:
    """Turn the README's text into the home page's text."""
    body = DOCS_LINK_RE.sub(r"](\1", readme)
    body = DOCS_SRC_RE.sub(r'\1="', body)
    return BANNER + body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if docs/index.md is not what this would write, and write nothing",
    )
    args = parser.parse_args(argv)

    wanted = render(README.read_text(encoding="utf-8"))
    current = INDEX.read_text(encoding="utf-8") if INDEX.exists() else None

    if args.check:
        if current == wanted:
            print(f"{INDEX.relative_to(ROOT)} is up to date with README.md")
            return 0
        print(
            f"{INDEX.relative_to(ROOT)} is stale — README.md has changed since it was generated.\n"
            "Run: python tools/build_docs_index.py",
            file=sys.stderr,
        )
        return 1

    if current == wanted:
        print(f"{INDEX.relative_to(ROOT)} already up to date")
        return 0
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(wanted, encoding="utf-8")
    print(f"wrote {INDEX.relative_to(ROOT)} from README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
