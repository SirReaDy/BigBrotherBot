"""Build-time link rewriting, so the same markdown reads correctly on GitHub and on the site.

Every page in `docs/` opens with a navigation row that links back to `../README.md`, because that is
where the overview lives when you are reading the file in the repository. Inside the built site the
overview *is* `index.md` — `README.md` is not part of `docs/` and never will be — so those links have
no target and `mkdocs build --strict` refuses to publish, which is exactly what it should do.

The fix cannot be to edit the files: they have to keep working on GitHub, which is where most people
will meet them first. So the rewrite happens here, on the markdown mkdocs is about to render, and
nothing on disk changes. It is the same transform `tools/build_docs_index.py` applies in the other
direction when it turns the README into the home page.

Registered by `hooks:` in `mkdocs.yml`. Nothing imports this module.
"""

from __future__ import annotations

import re
from typing import Any

#: `](../README.md)` and `](../README.md#supported-games)` -> `](index.md)`, keeping any anchor.
#: Anchored on the closing bracket of a link's text so that prose *mentioning* the file is untouched;
#: only a link target moves.
README_LINK_RE = re.compile(r"\]\(\.\./README\.md(?P<anchor>#[^)]*)?\)")

#: The line the README opens with, pointing at this site. It is how a reader of the repository finds
#: these pages at all; on these pages it is a link to where they already are, so it goes — the same
#: argument as the navigation row below, and the same mechanism.
SITE_LINK_RE = re.compile(
    r"^\*\*\[Read the documentation\]\([^)]*\)\*\*[^\n]*\n(?:[^\n]+\n)*\n", re.MULTILINE
)

#: The hand-written navigation row every page opens with — eight cells linking to the other seven
#: pages, with this one in bold — and its separator line.
#:
#: **Dropped from the site, and it is not only a tidy-up.** Eight columns do not fit the content
#: width, so the table scrolls sideways, and being the first thing on the page that puts a horizontal
#: scrollbar across the top of all eight of them. On the site it is redundant as well as ugly:
#: Material already carries the same navigation in the sidebar, the header and the table of contents.
#:
#: It stays in the files, because on GitHub it is the *only* navigation there is — a reader who opens
#: `docs/games.md` in the repository has no sidebar and no way to reach the rest. Which is the same
#: reason the README link above is rewritten rather than edited: one source, read in two places that
#: offer different things around it.
NAV_ROW_RE = re.compile(
    r"^\|\s*(?:\[Overview\]\([^)]*\)|\*\*Overview\*\*)\s*\|.*\n\|(?:\s*-+\s*\|)+\s*\n",
    re.MULTILINE,
)


def on_page_markdown(markdown: str, **_kwargs: Any) -> str:
    """Make one markdown file read correctly in the two places it is published."""
    markdown = README_LINK_RE.sub(lambda m: f"](index.md{m['anchor'] or ''})", markdown)
    markdown = SITE_LINK_RE.sub("", markdown, count=1)
    return NAV_ROW_RE.sub("", markdown, count=1)
