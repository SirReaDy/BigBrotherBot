"""Render a captured terminal session to a self-contained animated SVG.

The home page needs to *show* `b3 doctor` diagnosing a server, because that is the most convincing
thing this project has: it is the bot telling an operator what is wrong with their setup, rather than
a paragraph claiming it would. A still screenshot does not carry that, and the usual ways to animate
one do not suit this repository:

* **asciinema.org embeds** put a third-party script and somebody else's hosting on every page view —
  the same class of dependency this rewrite spent its life deleting, and the reason `b3/update.py`
  and the publist plugin are gone.
* **`agg` or `svg-term`** produce a fine self-contained SVG and need Node to do it. There is no Node
  anywhere in this project, and adding a toolchain for one image is a poor trade.

So the animation is built here, from text. That has a property the recorded kinds do not: when the
output changes, the picture is **regenerated** rather than re-recorded by hand — so it cannot quietly
go on showing a version of the CLI that no longer exists.

**SMIL, not JavaScript.** An SVG referenced by `<img src=…>` may not run script, which is exactly how
the page embeds it; SMIL animation still runs there. Every line is drawn once and revealed by an
`<animate>` on its opacity, with `keyTimes` placing the reveal — so the whole session loops with no
state to reset.

The input is the session as it was captured, with `$ ` marking what was typed::

    $ b3 doctor
    [  ok  ] game                cod4, server 127.0.0.1:28960
    [ FAIL ] rcon                no reply from 127.0.0.1:28960

Usage::

    python tools/termsvg.py docs/assets/doctor.session -o docs/assets/doctor.svg
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

#: Terminal geometry, in pixels. The character width is the advance of the monospace stack below at
#: this size; it only has to be consistent, since nothing is positioned relative to text it does not
#: itself place.
FONT_SIZE = 14
CHAR_WIDTH = 8.4
LINE_HEIGHT = 21
PADDING = 18
#: Room for the three window dots, so the first line is not under them.
CHROME_HEIGHT = 30

#: Seconds. A typed line appears a character at a time; output arrives in a burst, the way a command
#: that has finished thinking prints it.
TYPING_PER_CHAR = 0.045
PAUSE_BEFORE_OUTPUT = 0.55
OUTPUT_LINE_DELAY = 0.11
HOLD_AT_END = 3.5

#: One dark palette in both colour schemes. A terminal that turns white in light mode reads as a
#: document rather than as a program, and the point of the picture is that this is a program.
BACKGROUND = "#12161c"
CHROME = "#1b212a"
FOREGROUND = "#c8d2de"
DIM = "#7d8794"
PROMPT = "#7aa2f7"
TYPED = "#e6edf5"

#: Spans coloured by what they say. Deliberately small: this highlights the *verdicts*, which is what
#: a reader's eye should land on, and leaves prose alone.
TOKENS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\[\s*ok\s*\]"), "#3fb950"),
    (re.compile(r"\[\s*(FAIL|ERROR)\s*\]"), "#f85149"),
    (re.compile(r"\[\s*(WARN|warn)\s*\]"), "#d29922"),
    (re.compile(r"^\s*->.*$"), "#d29922"),
    (re.compile(r"\d+ problem\(s\).*$"), "#f85149"),
]


@dataclass(frozen=True, slots=True)
class Line:
    """One rendered line, and the moment it appears."""

    text: str
    at: float
    typed: bool


def parse_session(source: str) -> list[Line]:
    """Turn a captured session into timed lines.

    A line starting with ``$ `` is something a person typed, and is given a typing delay in
    proportion to its length; everything else is output, which arrives in a quick cascade after a
    beat. The result is the shape of a real session without pretending to have measured one — the
    text is real, the *timing* is a reading aid and is not claimed to be anything else.
    """
    lines: list[Line] = []
    clock = 0.4
    previous_was_output = False
    for raw in source.splitlines():
        text = raw.rstrip("\n")
        if text.startswith("$ "):
            if previous_was_output:
                clock += 0.9  # a beat between one command finishing and the next being typed
            lines.append(Line(text, clock, typed=True))
            clock += len(text) * TYPING_PER_CHAR + PAUSE_BEFORE_OUTPUT
            previous_was_output = False
            continue
        lines.append(Line(text, clock, typed=False))
        clock += OUTPUT_LINE_DELAY
        previous_was_output = True
    return lines


def spans(text: str, base: str) -> str:
    """Colour the verdicts in a line, leaving the rest at ``base``.

    Whitespace is preserved with ``xml:space``, and every run is emitted as its own `tspan` so the
    monospace grid is never rebuilt from character counts — the renderer lays it out.
    """
    for pattern, colour in TOKENS:
        match = pattern.search(text)
        if match is None:
            continue
        before, hit, after = (
            text[: match.start()],
            text[match.start() : match.end()],
            text[match.end() :],
        )
        parts = []
        if before:
            parts.append(f'<tspan fill="{base}">{escape(before)}</tspan>')
        parts.append(f'<tspan fill="{colour}">{escape(hit)}</tspan>')
        if after:
            parts.append(f'<tspan fill="{base}">{escape(after)}</tspan>')
        return "".join(parts)
    return f'<tspan fill="{base}">{escape(text)}</tspan>'


def render(lines: list[Line], title: str) -> str:
    """Build the SVG. Self-contained: no script, no external font, no network."""
    total = (max((line.at for line in lines), default=0.0)) + HOLD_AT_END
    columns = max((len(line.text) for line in lines), default=40)
    width = int(PADDING * 2 + columns * CHAR_WIDTH)
    height = int(CHROME_HEIGHT + PADDING + len(lines) * LINE_HEIGHT + PADDING)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">',
        f"<title>{escape(title)}</title>",
        # The whole session as text, for a screen reader and for anybody who cannot see the
        # animation at all — the alternative is an image that says nothing to either.
        f"<desc>{escape(chr(10).join(line.text for line in lines))}</desc>",
        '<style>text{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,'
        f'"DejaVu Sans Mono",monospace;font-size:{FONT_SIZE}px}}</style>',
        f'<rect width="{width}" height="{height}" rx="8" fill="{BACKGROUND}"/>',
        f'<rect width="{width}" height="{CHROME_HEIGHT}" rx="8" fill="{CHROME}"/>',
        f'<rect y="{CHROME_HEIGHT - 8}" width="{width}" height="8" fill="{CHROME}"/>',
    ]
    for index, colour in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        out.append(
            f'<circle cx="{18 + index * 18}" cy="{CHROME_HEIGHT / 2}" r="5" fill="{colour}"/>'
        )

    for number, line in enumerate(lines):
        y = CHROME_HEIGHT + PADDING + number * LINE_HEIGHT
        start = line.at / total if total else 0.0
        if line.typed:
            body = (
                f'<tspan fill="{PROMPT}">$ </tspan>'
                f'<tspan fill="{TYPED}">{escape(line.text[2:])}</tspan>'
            )
        else:
            body = spans(line.text, DIM if line.text.startswith("    ") else FOREGROUND)
        out.append(
            # `opacity="1"` is the *fallback*, and it matters. A running animation overrides the
            # attribute, so where SMIL works the line starts hidden and is revealed on cue; where it
            # does not — GitHub has switched SMIL off before, and some readers never had it — the
            # attribute stands and the picture is the finished session. The alternative, starting at
            # 0, degrades to an empty terminal, which is worse than no image at all.
            f'<text x="{PADDING}" y="{y}" xml:space="preserve" opacity="1">{body}'
            # values/keyTimes rather than begin=: an `animate` that ends is not a loop, and this way
            # the line's whole life — hidden, then shown, then the restart — is one declaration.
            f'<animate attributeName="opacity" values="0;0;1;1" '
            f'keyTimes="0;{start:.4f};{start:.4f};1" dur="{total:.2f}s" '
            f'repeatCount="indefinite"/></text>'
        )

    cursor_y = CHROME_HEIGHT + PADDING + len(lines) * LINE_HEIGHT - FONT_SIZE + 3
    out.append(
        f'<rect x="{PADDING}" y="{cursor_y}" width="{CHAR_WIDTH:.1f}" height="{FONT_SIZE}" '
        f'fill="{FOREGROUND}"><animate attributeName="opacity" values="1;1;0;0;1" '
        f'keyTimes="0;0.25;0.26;0.75;1" dur="1.1s" repeatCount="indefinite"/></rect>'
    )
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "session", type=Path, help="captured session, with `$ ` marking what was typed"
    )
    parser.add_argument("-o", "--output", type=Path, required=True, help="SVG to write")
    parser.add_argument(
        "--title", default="", help="accessible name; defaults to the first command"
    )
    args = parser.parse_args(argv)

    lines = parse_session(args.session.read_text(encoding="utf-8"))
    if not lines:
        parser.error(f"{args.session} has no lines")
    title = args.title or next((line.text[2:] for line in lines if line.typed), args.session.stem)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(lines, title), encoding="utf-8")
    print(f"wrote {args.output} ({len(lines)} lines, {args.output.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
