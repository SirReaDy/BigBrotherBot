"""`b3 probe` — the diagnostic that turns "what does your server actually say?" into one paste.

The properties worth pinning are not really about formatting. They are: it must be **read-only** (an
operator runs this on a live server), it must show the raw reply *and* which of our candidate patterns
matched it, it must separate "no rows" from "rows we could not read", and it must be able to mask
addresses and ids for somebody about to paste the output in public.
"""

from __future__ import annotations

import pytest

from b3.config.schema import BotConfig, Config, ServerConfig
from b3.core.probe import redact, run_probe

STEAM = "76561198000000001"

#: A CoD4X table *without* the Steam64 column — the shape that used to be mis-parsed, and the case
#: the candidate-by-candidate report exists to make obvious.
NO_STEAM_STATUS = """map: mp_crash
num score ping guid     name             lastmsg address               qport rate
--- ----- ---- -------- ---------------- ------- --------------------- ----- -----
  0    12   47   123456 ^1Bob^7                50 192.0.2.44:28960      12345 25000
"""

WITH_STEAM_STATUS = f"""map: mp_crash
num score ping guid    steamid           name    address
--- ----- ---- ------- ----------------- ------- ------------------
  0    12   47 1234567 {STEAM} Bob     192.0.2.44:28960
"""


class ScriptedRcon:
    """Answers what it is told to, and records what it was asked."""

    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.commands: list[str] = []
        self.closed = False

    def command(self, cmd: str) -> str:
        self.commands.append(cmd)
        return self.replies.get(cmd, "")

    def close(self) -> None:
        self.closed = True


def _config(tmp_path, game="cod4x", log_lines=(), password="secret"):  # noqa: ANN001, ANN202
    log = tmp_path / "games_mp.log"
    log.write_text("".join(f"{line}\n" for line in log_lines), encoding="latin-1")
    return Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(
            game=game, rcon_password=password, game_log=str(log), encoding="latin-1"
        ),
    )


def _report(config, **kwargs) -> str:  # noqa: ANN001
    return "\n".join(run_probe(config, **kwargs))


# -- the status section -------------------------------------------------------------------------


def test_it_shows_the_raw_reply_not_only_its_conclusion(tmp_path):
    """The whole point: an operator pastes what the server said, not what we made of it."""
    rcon = ScriptedRcon({"status": WITH_STEAM_STATUS})
    report = _report(_config(tmp_path), rcon_factory=lambda: rcon)

    assert "num score ping guid    steamid" in report  # the header, verbatim
    assert "parsed 1 player(s)" in report
    assert f"guid={STEAM}" in report
    assert "identity is the `steam` column" in report


def test_it_says_which_candidate_pattern_matched_and_which_did_not(tmp_path):
    """"The strict shape matched 0 rows and the one without a steam column matched 1" is the sentence
    that identifies a changed table at a glance. Candidates are named by their *columns*, because
    every one of these patterns starts `^\\s*(?P<slot>...` and a regex excerpt distinguishes nothing."""
    rcon = ScriptedRcon({"status": NO_STEAM_STATUS})
    report = _report(_config(tmp_path), rcon_factory=lambda: rcon)

    assert "candidate 1:   0 row(s) matched" in report
    assert "candidate 2:   1 row(s) matched" in report
    assert "steam" in report.split("candidate 1:")[1].split("\n")[0]  # names its columns


def test_every_status_command_is_reported_not_just_the_winner(tmp_path):
    """CoD4X asks `b3status` first. Whether *that* server knows it is the open question, so the
    report has to show the attempt and its answer, not silently fall back."""
    rcon = ScriptedRcon({"b3status": 'unknown command "b3status"', "status": WITH_STEAM_STATUS})
    report = _report(_config(tmp_path), rcon_factory=lambda: rcon)

    assert rcon.commands[:2] == ["b3status", "status"]
    assert 'unknown command "b3status"' in report
    assert "asked: status" in report


def test_rows_it_cannot_read_are_reported(tmp_path):
    """An empty server and an unrecognised table shape both parse to zero players."""
    unknown = (
        "map: mp_crash\n"
        "num score ping something name\n"
        "  0    12   47 ??        Bob\n"
    )
    rcon = ScriptedRcon({"b3status": unknown, "status": unknown})
    report = _report(_config(tmp_path), rcon_factory=lambda: rcon)

    assert "parsed 0 players" in report
    assert "match no known pattern" in report
    assert "! 0    12   47 ??        Bob" in report


def test_an_empty_server_is_reported_as_empty(tmp_path):
    rcon = ScriptedRcon({"b3status": "map: mp_crash\n", "status": "map: mp_crash\n"})
    report = _report(_config(tmp_path), rcon_factory=lambda: rcon)

    assert "parsed 0 players" in report
    assert "probably empty" in report
    assert "THIS IS THE BUG" not in report


def test_without_an_rcon_password_it_says_so_rather_than_failing(tmp_path):
    report = _report(_config(tmp_path, password=""))
    assert "no rcon_password set" in report


# -- the cvar section ---------------------------------------------------------------------------


def test_the_cvar_read_shows_the_form_it_asked_in(tmp_path):
    """Two titles in one family disagree about this, so the *question* matters as much as the answer."""
    rcon = ScriptedRcon({"sv_maxclients": '"sv_maxclients" is: "32^7" default: "16^7"'})
    report = _report(_config(tmp_path), rcon_factory=lambda: rcon)

    assert "asked: sv_maxclients" in report
    assert "parsed: 32" in report


def test_an_unparsed_cvar_reply_is_named_as_such(tmp_path):
    """The Plutonium IW5 case: the server answers plainly and our pattern does not fit the answer."""
    rcon = ScriptedRcon({"sv_maxclients": "sv_maxclients is 32"})  # no quotes at all
    report = _report(_config(tmp_path), rcon_factory=lambda: rcon)

    assert "parsed: NOTHING" in report


def test_the_cvar_asked_can_be_chosen(tmp_path):
    rcon = ScriptedRcon({"mapname": '"mapname" is: "mp_crash"'})
    report = _report(_config(tmp_path), cvar="mapname", rcon_factory=lambda: rcon)
    assert "asked: mapname" in report
    assert "parsed: mp_crash" in report


# -- the log section ----------------------------------------------------------------------------


def test_it_names_the_handler_each_line_matched(tmp_path):
    config = _config(
        tmp_path,
        log_lines=[
            r"  0:30 InitGame: \g_gametype\dm\mapname\mp_crash",
            f"  0:12 J;{'a' * 32};0;Bob",
            "  0:15 say;x;0;Bob;hello",
        ],
    )
    report = _report(config, rcon_factory=lambda: ScriptedRcon({}))

    assert "matched   3" in report
    assert "on_init_game" in report
    assert "on_join" in report
    assert "on_say" in report
    assert "UNMATCHED 0" in report


def test_lines_that_match_nothing_are_the_headline(tmp_path):
    """This is the section that answers "is our grammar complete for this title?" — the question that
    went unasked for as long as most of Urban Terror's weapons were invisible."""
    config = _config(
        tmp_path,
        log_lines=[
            f"  0:12 J;{'a' * 32};0;Bob",
            "  0:25 Weapon;xyz;0;Bob;deserteagle_mp",
            "  0:26 ScriptKill;xyz;0;whatever",
        ],
    )
    report = _report(config, rcon_factory=lambda: ScriptedRcon({}))

    assert "UNMATCHED 2" in report
    assert "! " in report
    assert "Weapon;xyz;0;Bob;deserteagle_mp" in report
    assert "ScriptKill" in report


def test_a_missing_log_is_reported_not_raised(tmp_path):
    config = _config(tmp_path)
    config.server.game_log = str(tmp_path / "nope.log")
    report = _report(config, rcon_factory=lambda: ScriptedRcon({}))
    assert "could not read" in report


@pytest.mark.parametrize("game", ["bf3", "arma3"])
def test_a_pushed_engine_has_no_log_to_read(tmp_path, game):
    report = _report(_config(tmp_path, game=game), rcon_factory=lambda: ScriptedRcon({}))
    assert "no log file" in report


# -- read-only, and safe to paste ---------------------------------------------------------------


def test_it_never_sends_anything_that_changes_the_server(tmp_path):
    """An operator runs this on a live server. There must be no path from here to a kick, a ban or a
    line of chat — so the whole set of commands sent is asserted, not sampled."""
    rcon = ScriptedRcon({"b3status": WITH_STEAM_STATUS})
    _report(_config(tmp_path), rcon_factory=lambda: rcon)

    assert rcon.commands == ["b3status", "status", "sv_maxclients"]
    forbidden = ("kick", "ban", "say", "tell", "map", "set ", "quit")
    assert not [c for c in rcon.commands if c.lower().startswith(forbidden)]


def test_both_status_commands_are_asked_even_when_the_first_one_answers(tmp_path):
    """Not waste — the comparison *is* a diagnostic. If `b3status` lists five players and `status`
    lists four, the b3hide mod is installed and hiding one, which is precisely the question TODO §1.1
    could not answer without a live server."""
    rcon = ScriptedRcon({"b3status": WITH_STEAM_STATUS, "status": "map: mp_crash\n"})
    report = _report(_config(tmp_path), rcon_factory=lambda: rcon)

    assert rcon.commands[:2] == ["b3status", "status"]
    assert "parsed 1 player(s)" in report  # what b3status said
    assert "probably empty" in report  # and what plain status said, side by side


def test_altitude_is_never_connected_to_at_all(tmp_path):
    """Its "rcon" is a command file, and *opening* that clears it — a side effect a diagnostic must
    not have. There is also nothing to ask: the engine answers nothing."""
    command_file = tmp_path / "command.txt"
    command_file.write_text("27276,console,kick Bob\n", encoding="utf-8")
    config = _config(tmp_path, game="altitude", password="")
    config.server.command_file = str(command_file)

    report = _report(config)

    assert "cannot be queried at all" in report
    assert command_file.read_text(encoding="utf-8") == "27276,console,kick Bob\n"  # untouched


def test_redact_masks_addresses_and_ids_but_keeps_map_names():
    line = "cid=0 name='Bob' guid=76561198000000001 ip=192.0.2.44:28960 map=mp_backlot"
    masked = redact(line)

    assert "76561198000000001" not in masked
    assert "7656..." in masked
    assert "192.0.2.44" not in masked
    assert "x.x.x.x" in masked
    assert "mp_backlot" in masked  # a report with the map blanked out is much less use
    assert "28960" in masked  # a port is not an identity


def test_the_report_can_be_redacted_end_to_end(tmp_path):
    rcon = ScriptedRcon({"b3status": WITH_STEAM_STATUS})
    report = _report(_config(tmp_path), hide=True, rcon_factory=lambda: rcon)

    assert STEAM not in report
    assert "192.0.2.44" not in report
    assert "addresses and ids are masked" in report


def test_the_report_is_ascii_so_it_survives_any_console(tmp_path):
    """A Windows console is often still cp1252, where a box-drawing character raises
    UnicodeEncodeError — and a diagnostic that crashes while describing a server is worse than none."""
    rcon = ScriptedRcon({"b3status": WITH_STEAM_STATUS})
    report = _report(_config(tmp_path), rcon_factory=lambda: rcon)

    report.encode("cp1252")  # raises if anything in it is not encodable
    assert report.isascii()


def test_the_rcon_client_is_closed(tmp_path):
    rcon = ScriptedRcon({"b3status": WITH_STEAM_STATUS})
    _report(_config(tmp_path), rcon_factory=lambda: rcon)
    assert rcon.closed is True
