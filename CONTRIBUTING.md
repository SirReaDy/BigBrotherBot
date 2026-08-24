# Contributing

## Getting set up

```bash
python -m pip install -e ".[dev]"
pytest
ruff check src tests tools
mypy src
python tools/check_links.py
python tools/check_counts.py
```

The first three have to pass: `mypy` runs in strict mode over `src`, and `ruff` is configured for a
100-column line length and Python 3.11.

The last two check the documents rather than the code, and both exist because their failure is
**silent**. A wrong relative link renders as an ordinary link and scrolls nowhere;
`tools/check_links.py` finds it. A wrong *number* is worse, because it reads as a measurement — this
repository has already carried a test count of 1,983 against an actual 2,525 —  so
`tools/check_counts.py` measures the tests, the test files, the lines and the bundled plugins and
fails when a document disagrees. Counts have to match exactly; line totals may drift by 5%, since a
figure that has to be edited on every commit gets edited carelessly instead.

## Adding a game title

Most contributions are a new title, and the order matters more than it looks like it should. Each of
these three steps catches a class of mistake the other two cannot, so none of them is skippable
because the previous one was clean.

**1. Read the classic bot's captured tests first.** The
[original project](https://github.com/BigBrotherBot/big-brother-bot) has real log lines and real
server replies under `tests/core/parsers/`. Run one through your parser before you write a handler
for it. Every title added this way has turned up something a careful reading of the classic parser
got wrong, and a parser existing in that tree is not evidence that it ever ran.

**2. Write a fake server before the parser.** There are ten under `tools/fakeservers/` to copy from.
Every one of them found a real bug on its first run. Be aware of the limit, though: a fake written
from the same reading as the client will agree with the client, so only captured data settles what
the protocol actually is.

**3. Then the parser, then an end-to-end driver.** The `e2e_*.py` scripts drive a real bot against a
fake server. They exist because a unit test asserts what was *sent*, which is not the same as what it
accomplished: the Source work shipped a ban verb that recorded the ban, satisfied every unit test,
and left the player on the server.

A title is usually a `GameProfile` in the family's `profiles.py`, not a new parser class. If you find
yourself subclassing to change strings, the strings belong in the profile.

## Style

- **Comments say what the code does and why**, in the present tense. No references to issue numbers
  or planning documents, no narration of how the code came to be, and no "this is broken" notes: a
  comment nobody can act on is not documentation. If something does not work, open an issue.
- **A silent failure is the bug to design against.** Most faults found in this project were not
  crashes, they were a pattern matching nothing, which is indistinguishable from a quiet server.
  Prefer a loud refusal at startup over a bot that runs and does nothing.
- Player-supplied values go through `sanitize_rcon_value` before they reach a command.
- Test names describe the behaviour, not the fix.

## Pull requests

Keep a PR to one change, with tests. Say which game and which engine you ran it against, and whether
that was a real server or a fake, since the two are very different evidence.

## Cutting a release

Versions are `vX.Y.Z` tags on this repository, and **three things have to agree**: the tag,
`project.version` in `pyproject.toml`, and `b3.__version__`. The release workflow refuses a tag where
they do not, because `b3 update` compares the running `b3.__version__` against the highest tag here —
a disagreement makes the bot either miss an update or offer one that installs the same code.

```bash
# 1. bump both, in one commit
#    pyproject.toml:   version = "2.1.0"
#    src/b3/__init__.py: __version__ = "2.1.0"
git commit -am "chore: 2.1.0"
git tag v2.1.0
git push origin main v2.1.0
```

The `Release` workflow then checks the three agree, builds a wheel and an sdist, installs the wheel
into a clean virtualenv and runs it, and attaches both files to the GitHub release.

**A pre-release tag is not offered as an update.** `v2.1.0-rc1`, `v2.2.0b1`, `2.0.0a0` — tag them,
push them, install one with `b3 update --to v2.1.0-rc1`; but the update line every operator sees names
the newest **final** release, and nothing else. That is deliberate: a candidate that could be offered
would go on being offered after the release it was a candidate for had shipped. The same ordering runs
the other way, so somebody running `2.0.0a0` *is* offered `2.0.0` when it lands.

**There is no PyPI**, by decision: the source of truth is this repository, and installing is
`pip install git+https://github.com/SirReaDy/BigBrotherBot@v2.1.0` — which needs nobody's permission
and is the same distribution story as `b3 plugin install`, which pins plugins by tag from git. The
built files exist so an install needs no git checkout, not because anything is published elsewhere.
