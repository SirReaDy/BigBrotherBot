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

**2. Write a fake server before the parser.** There are seven under `tools/fakeservers/` to copy
from, and eight `e2e_*.py` drivers over them. Every one of them found a real bug on its first run.
Be aware of the limit, though: a fake written from the same reading as the client will agree with
the client, so only captured data settles what the protocol actually is.

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

**A pull request from a fork is how an outside change gets in.** Contributors have no write access
to this repository: you cannot push a branch here, merge your own PR, or create a tag or a release.
That is not a comment on anybody's work — it is what keeps `main` and the version tags to one path,
since `b3 update` points every running bot at the tags on this remote and treats them as the truth.

So the loop is:

```bash
# fork on GitHub, then
git clone https://github.com/<you>/BigBrotherBot && cd BigBrotherBot
git checkout -b a-short-branch-name
python -m pip install -e ".[dev]"
# ... your change, with tests ...
git push origin a-short-branch-name   # your fork, not this repository
```

**`main` is the only long-lived branch** — there is no `develop`, no release branch and no
per-version branch. Fork from it, rebase on it, and open the PR against it; a release is a tag on
`main`, not a branch cut from it.

CI runs on the pull request itself — ruff, `mypy --strict`, the test suite, the eight end-to-end
drivers, and the document checks — and every job has to be green before a maintainer will merge
it. Run them locally first; the list is at the top of this file, and finding out from CI what
`ruff` would have told you in two seconds wastes your round-trip, not ours.

What makes a PR easy to merge:

- **One change.** A fix and a refactor in the same branch means neither can be taken on its own.
- **Tests, named for the behaviour** rather than for the fix.
- **Say what you ran it against** — which game, which engine, and whether that was a real server or a
  fake. The two are very different evidence, and a fake written from the same reading as the code
  will agree with the code.
- **Do not bump the version.** `pyproject.toml` and `b3.__version__` are moved by the release commit
  and nothing else; a bump in a PR conflicts with the next one and cannot be merged as-is.
- **Do not edit generated tables by hand.** `python tools/gen_docs_tables.py` writes them from the
  code; `--check` fails the build when they drift.

`main` has no merge commits in it and is meant to stay that way, so your PR arrives as one commit
with your description as its message. Write that description as the thing you would want to find in
the log a year from now — what changed and why, not what you did to the branch.

## Conduct

[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) - Contributor Covenant 2.1. Reports go to the maintainer
privately, through a [security advisory](https://github.com/SirReaDy/BigBrotherBot/security/advisories/new)
or a message to [@SirReaDy](https://github.com/SirReaDy).

## Licensing of what you send

By opening a pull request you agree your contribution is licensed under
[GPL-2.0-or-later](LICENSE), the same terms as the rest of the project. There is no CLA and no
copyright assignment: you keep the copyright in what you wrote.

One thing this rules out, and it comes up because of where the material here comes from: **do not
paste code from a source whose licence you have not checked.** Reading the classic bot is
encouraged — it is GPL-2.0 and this is a derived work — but a third-party plugin or a snippet from
a forum post may be under anything at all, and a fix has to be re-implemented rather than copied if
its licence is unknown. Say in the PR where a log line or a protocol detail came from; captured
data is evidence, not code, and citing it helps the next person.

## Cutting a release — maintainers only

This section is here so the process is not a secret, not because it is a step in contributing:
pushing to `main` and creating tags need write access, so a release is a maintainer action. If you
believe a release is due, say so in an issue.

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
