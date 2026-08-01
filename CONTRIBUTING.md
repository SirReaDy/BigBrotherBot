# Contributing

## Getting set up

```bash
python -m pip install -e ".[dev]"
pytest
ruff check src tests tools
mypy src
```

All three have to pass. `mypy` runs in strict mode over `src`, and `ruff` is configured for a
100-column line length and Python 3.11.

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
