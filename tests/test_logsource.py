"""Log tailer: tailing, partial-line buffering, rotation/truncation, tail-from-end."""

from __future__ import annotations

from pathlib import Path

from b3.net.logsource import FileLogSource


def _write(path: Path, data: str, mode: str = "a") -> None:
    with open(path, mode, encoding="latin-1", newline="") as fh:
        fh.write(data)


def test_reads_existing_lines_from_start(tmp_path):
    log = tmp_path / "games_mp.log"
    _write(log, "line1\nline2\n", mode="w")

    src = FileLogSource(log, from_start=True)
    src.open()
    assert src.read_lines() == ["line1", "line2"]
    assert src.read_lines() == []  # nothing new
    src.close()


def test_appended_lines_are_reported(tmp_path):
    log = tmp_path / "games_mp.log"
    _write(log, "line1\n", mode="w")
    src = FileLogSource(log, from_start=True)
    src.open()
    assert src.read_lines() == ["line1"]

    _write(log, "line2\nline3\n")
    assert src.read_lines() == ["line2", "line3"]
    src.close()


def test_partial_line_is_buffered(tmp_path):
    log = tmp_path / "games_mp.log"
    _write(log, "", mode="w")
    src = FileLogSource(log, from_start=True)
    src.open()

    _write(log, "par")  # no newline yet
    assert src.read_lines() == []  # incomplete line held back

    _write(log, "tial\n")
    assert src.read_lines() == ["partial"]  # completed
    src.close()


def test_rotation_truncation_reseeks(tmp_path):
    log = tmp_path / "games_mp.log"
    _write(log, "old1\nold2\n", mode="w")
    src = FileLogSource(log, from_start=True)
    src.open()
    assert src.read_lines() == ["old1", "old2"]

    # Simulate rotation: the file is replaced by a shorter one.
    _write(log, "fresh\n", mode="w")
    assert src.read_lines() == ["fresh"]  # shrink detected, re-read from start
    src.close()


def test_tail_from_end_ignores_existing(tmp_path):
    log = tmp_path / "games_mp.log"
    _write(log, "history1\nhistory2\n", mode="w")

    src = FileLogSource(log, from_start=False)  # start at EOF
    src.open()
    assert src.read_lines() == []  # existing history not reported

    _write(log, "new\n")
    assert src.read_lines() == ["new"]
    src.close()


def test_latin1_decoding(tmp_path):
    log = tmp_path / "games_mp.log"
    with open(log, "wb") as fh:
        fh.write(b"Ren\xe9 joined\n")  # latin-1 'René'
    src = FileLogSource(log, from_start=True, encoding="latin-1")
    src.open()
    assert src.read_lines() == ["René joined"]
    src.close()
