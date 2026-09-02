<!--
Delete whatever does not apply. A one-line typo fix does not need a section per heading.
-->

## What this changes, and why

<!-- The why is the part a diff cannot show. If it fixes something, say what the symptom was. -->

## What you ran it against

<!--
The one thing a checklist cannot infer, and `CONTRIBUTING.md` asks for it for a reason: which game,
which engine or mod, and whether that was a **real server** or a fake / a replayed log.

A fake server is written from the same reading of the protocol as the code it tests, so both can be
wrong together. Two evenings against one real CoD4X server have found a dozen faults that thousands
of tests and eight fake servers agreed were fine. "Fakes only" is a perfectly good answer - it is
just different evidence, and saying so tells a reviewer where to look.
-->

## Checks

<!-- Which of these you ran, and anything you could not - "no Windows box" is a useful answer. -->

- [ ] `python -m pytest`
- [ ] `python -m ruff check src tests tools` and `ruff format --check`
- [ ] `python -m mypy src`
- [ ] `python tools/check_counts.py` and `python tools/check_links.py` - if you added a test file, a
      plugin, or a document that quotes a number, one of these will have something to say
- [ ] Tests covering the change, and for a fix, one that fails without it
