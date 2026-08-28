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


def on_page_markdown(markdown: str, **_kwargs: Any) -> str:
    """Point every link back to the overview at the page the overview became."""
    return README_LINK_RE.sub(lambda m: f"](index.md{m['anchor'] or ''})", markdown)
