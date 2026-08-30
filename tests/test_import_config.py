"""`b3 import-config` — a classic B3 configuration into this one's, and the report of what it did not.

The report is the feature under test as much as the conversion is. A converter that copied every
line would produce a file that looks complete and is quietly wrong in the few places where the same
word means something different, and a plausible-looking config is worse than an obviously incomplete
one: you stop looking at it.

Every sample here is the real shape from `b3/conf/` in the classic tree.
"""

from __future__ import annotations

import yaml

from b3.legacy.config import convert_config_tree, convert_main, convert_plugin

CLASSIC_MAIN = """<configuration>
    <settings name="b3">
        <set name="parser">cod4</set>
        <set name="database">mysql://b3:password@localhost/b3</set>
        <set name="bot_name">b3</set>
        <set name="time_zone">CST</set>
        <set name="log_level">9</set>
        <set name="logfile">b3.log</set>
    </settings>
    <settings name="server">
        <set name="rcon_password">secret</set>
        <set name="port">28960</set>
        <set name="game_log">games_mp.log</set>
        <set name="rcon_ip">127.0.0.1</set>
        <set name="punkbuster">on</set>
        <set name="delay">0.33</set>
    </settings>
    <settings name="messages">
        <set name="kicked_by">$clientname^7 was kicked by $adminname^7 $reason</set>
    </settings>
    <settings name="plugins">
        <set name="admin">@b3/conf/plugin_admin.ini</set>
        <set name="ftpytail">@b3/conf/plugin_ftpytail.ini</set>
    </settings>
</configuration>
"""

#: The real `[settings]` block of the classic `plugin_tk.ini`, plus its `levels` line — the one
#: setting in it that has no counterpart, because the classic named which groups get penalised and
#: here each level carries its own kill/damage/ban multipliers.
CLASSIC_TK = """[settings]
max_points: 400
levels: guest,user,reg,mod,admin
round_grace: 7
grudge_enable: yes
damage_threshold: 100
warn_duration: 1h
[messages]
ban: ^7team damage over limit
"""


def write(tmp_path, name, text):  # noqa: ANN001, ANN201
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# -- the main config -------------------------------------------------------


def test_the_main_config_converts_and_the_result_validates(tmp_path) -> None:  # noqa: ANN001
    from b3.config.schema import Config

    out = tmp_path / "out"
    out.mkdir()
    result = convert_main(write(tmp_path, "b3.xml", CLASSIC_MAIN), out)

    loaded = yaml.safe_load((out / "b3.yaml").read_text(encoding="utf-8"))
    config = Config(**loaded)  # the real schema, not a guess at it

    assert config.server.game == "cod4"
    assert config.server.rcon_password == "secret"
    assert config.server.port == 28960
    assert config.bot.name == "b3"
    assert result.target is not None


def test_a_mysql_url_gains_the_driver_sqlalchemy_needs(tmp_path) -> None:  # noqa: ANN001
    """`mysql://` is what the classic wrote and what SQLAlchemy will not accept.

    Rewritten rather than reported, because there is exactly one right answer — but reported *as
    well*, because it changes a connection string and an operator should know that happened.
    """
    out = tmp_path / "out"
    out.mkdir()
    result = convert_main(write(tmp_path, "b3.xml", CLASSIC_MAIN), out)

    loaded = yaml.safe_load((out / "b3.yaml").read_text(encoding="utf-8"))
    assert loaded["bot"]["database"].startswith("mysql+pymysql://")
    assert any("mysql+pymysql" in note for note in result.notes)


def test_on_becomes_a_boolean_rather_than_the_word(tmp_path) -> None:  # noqa: ANN001
    """`server.punkbuster` is typed; the string "on" is not a bool to YAML or to pydantic."""
    out = tmp_path / "out"
    out.mkdir()
    convert_main(write(tmp_path, "b3.xml", CLASSIC_MAIN), out)

    loaded = yaml.safe_load((out / "b3.yaml").read_text(encoding="utf-8"))
    assert loaded["server"]["punkbuster"] is True


def test_a_time_zone_abbreviation_is_reported_rather_than_guessed_at(tmp_path) -> None:  # noqa: ANN001
    """`CST` is US Central *and* China Standard — six hours apart.

    Every timestamp the bot writes depends on this, so it is the value not to guess at: left as
    written, and said out loud.
    """
    out = tmp_path / "out"
    out.mkdir()
    result = convert_main(write(tmp_path, "b3.xml", CLASSIC_MAIN), out)

    assert any("time_zone" in note and "abbreviation" in note for note in result.notes)


def test_a_setting_with_no_counterpart_says_why_it_has_none(tmp_path) -> None:  # noqa: ANN001
    """ "Not converted" and "you no longer need this" are different things to read mid-migration."""
    out = tmp_path / "out"
    out.mkdir()
    result = convert_main(write(tmp_path, "b3.xml", CLASSIC_MAIN), out)

    assert any("logfile" in n and "stdout" in n for n in result.notes)
    assert any("delay" in n and "event-driven" in n for n in result.notes)


def test_the_plugin_list_leaves_out_the_ones_that_are_core_now(tmp_path) -> None:  # noqa: ANN001
    """`ftpytail` in a classic config is `server.game_log` here, not a plugin to load."""
    out = tmp_path / "out"
    out.mkdir()
    convert_main(write(tmp_path, "b3.xml", CLASSIC_MAIN), out)

    loaded = yaml.safe_load((out / "b3.yaml").read_text(encoding="utf-8"))
    names = [entry["name"] for entry in loaded["plugins"]]
    assert names == ["admin"]


# -- plugin configs --------------------------------------------------------


def test_settings_the_plugin_still_has_are_converted_with_their_types(tmp_path) -> None:  # noqa: ANN001
    """An INI holds only strings; the YAML should look hand-written, because it will be edited."""
    out = tmp_path / "out"
    out.mkdir()
    convert_plugin(write(tmp_path, "plugin_tk.ini", CLASSIC_TK), out)

    loaded = yaml.safe_load((out / "plugin_tk.yaml").read_text(encoding="utf-8"))
    assert loaded["settings"]["max_points"] == 400  # an int, not "400"
    assert loaded["settings"]["grudge_enable"] is True  # a bool, not "yes"
    assert loaded["settings"]["warn_duration"] == "1h"  # and a duration is still a string


def test_a_setting_the_plugin_no_longer_has_is_reported_not_written(tmp_path) -> None:  # noqa: ANN001
    """`tk`'s `levels` was a list of groups; it is a per-level table of multipliers now.

    This is the case that makes a copy-everything converter dangerous: written through verbatim it
    is a key the plugin ignores, so the operator's real setting silently stays at its default.
    """
    out = tmp_path / "out"
    out.mkdir()
    result = convert_plugin(write(tmp_path, "plugin_tk.ini", CLASSIC_TK), out)

    loaded = yaml.safe_load((out / "plugin_tk.yaml").read_text(encoding="utf-8"))
    assert "levels" not in loaded["settings"]
    assert any("levels" in note for note in result.notes)


def test_a_section_that_moved_elsewhere_says_where(tmp_path) -> None:  # noqa: ANN001
    out = tmp_path / "out"
    out.mkdir()
    result = convert_plugin(write(tmp_path, "plugin_tk.ini", CLASSIC_TK), out)

    assert any("[messages]" in n and "b3.yaml" in n for n in result.notes)


def test_a_plugin_that_is_core_now_is_skipped_with_the_setting_that_replaced_it(tmp_path) -> None:  # noqa: ANN001
    out = tmp_path / "out"
    out.mkdir()
    result = convert_plugin(write(tmp_path, "plugin_ftpytail.ini", "[settings]\nurl: x\n"), out)

    assert result.target is None
    assert "server.game_log" in result.skipped


def test_a_dropped_plugin_is_skipped_with_the_reason(tmp_path) -> None:  # noqa: ANN001
    out = tmp_path / "out"
    out.mkdir()
    result = convert_plugin(write(tmp_path, "plugin_translator.ini", "[settings]\na: b\n"), out)

    assert result.target is None
    assert "README" in result.skipped


def test_an_operators_own_entries_are_copied_rather_than_checked(tmp_path) -> None:  # noqa: ANN001
    """`[spamages]` keys are the operator's words. `rule7` is neither known nor unknown — it is theirs.

    Checking these against the example config would discard somebody's server rules, which are
    exactly the lines they would most notice the loss of.
    """
    out = tmp_path / "out"
    out.mkdir()
    ini = "[spamages]\nrule7: ^3no advertising\nvent: ^3vent.example.com\n"
    result = convert_plugin(write(tmp_path, "plugin_admin.ini", ini), out)

    loaded = yaml.safe_load((out / "plugin_admin.yaml").read_text(encoding="utf-8"))
    assert loaded["spamages"] == {"rule7": "^3no advertising", "vent": "^3vent.example.com"}
    assert result.notes == []


def test_a_renamed_key_in_a_checked_section_is_reported(tmp_path) -> None:  # noqa: ANN001
    """`[warn]` *is* a schema, and four of its keys were renamed.

    `alert_kick_num` is `alert_at` now. Copied wholesale the operator would get a key the plugin
    ignores and a default where they had set a value — the same trap as `tk`'s `levels`, in the
    section right next to one that must be copied wholesale.
    """
    out = tmp_path / "out"
    out.mkdir()
    ini = "[warn]\nalert_kick_num: 3\ntempban_duration: 1d\n"
    result = convert_plugin(write(tmp_path, "plugin_admin.ini", ini), out)

    loaded = yaml.safe_load((out / "plugin_admin.yaml").read_text(encoding="utf-8"))
    assert loaded["warn"] == {"tempban_duration": "1d"}  # the one that still exists
    assert any("alert_kick_num" in note for note in result.notes)


def test_the_classic_prefix_settings_point_at_the_core_config(tmp_path) -> None:  # noqa: ANN001
    """ "the admin plugin has no such setting" is true and useless: they exist, one level up."""
    out = tmp_path / "out"
    out.mkdir()
    ini = "[settings]\nhidecmd_level: senioradmin\n"
    result = convert_plugin(write(tmp_path, "plugin_admin.ini", ini), out)

    assert any("bot.silent_level" in note for note in result.notes)


# -- the whole tree --------------------------------------------------------


def test_a_dry_run_reports_without_writing(tmp_path) -> None:  # noqa: ANN001
    source = tmp_path / "conf"
    source.mkdir()
    out = tmp_path / "out"
    write(source, "b3.xml", CLASSIC_MAIN)
    write(source, "plugin_tk.ini", CLASSIC_TK)

    report = convert_config_tree(source, out, write=False)

    assert report.written == 2  # it says what it *would* write
    assert list(out.glob("*.yaml")) == []
    assert "need your attention" in report.render()


# -- sections that moved rather than vanished ------------------------------

#: The real `plugin_spree.ini` tables. The classic wrote `%player%`; this reads `{player}`.
CLASSIC_SPREE = """[settings]
reset_spree: yes
[killingspree_messages]
5: %player% is on a killing spree # %player% stopped the spree of %victim%
[loosingspree_messages]
7: Keep it up %player% # You're back in business %player%
"""

#: The real `plugin_customcommands.ini` shape: the level is part of the *section name*.
CLASSIC_CUSTOMCOMMANDS = """[guest commands]
cookie = tell <ARG:FIND_PLAYER:PID> have a cookie
ns = tell <LAST_KILLER:PID> nice shot !
[admin commands]
slap = slap <ARG:FIND_PLAYER:PID>
[help]
cookie = give a cookie to a player
"""


def test_a_section_folded_into_settings_is_converted_not_reported(tmp_path) -> None:  # noqa: ANN001
    """Several plugins grew from a section per feature into one `settings:` block.

    Reporting these as unmappable was the wrong answer twice over: the keys are unchanged, and a
    file that converts nothing reads as "this plugin cannot be migrated" when in fact it is done.
    The plugin's own `DEFAULTS` is what tells a fold apart from a rename — the same authority
    `[settings]` is already checked against, so there is no second table to go stale.
    """
    out = tmp_path / "out"
    out.mkdir()
    ini = "[global_settings]\nimmunity_level: 100\nauto_update: yes\n"
    result = convert_plugin(write(tmp_path, "plugin_banlist.ini", ini), out)

    loaded = yaml.safe_load((out / "plugin_banlist.yaml").read_text(encoding="utf-8"))
    assert loaded["settings"] == {"immunity_level": 100, "auto_update": True}
    assert any("folded into `settings`" in note for note in result.notes)


def test_spree_message_tables_survive_with_their_placeholders_translated(tmp_path) -> None:  # noqa: ANN001
    """The operator's spree messages are their own writing, under a section name that changed.

    `%player%` is not a placeholder here — carried across untouched it is the literal text the
    server would print, which is the quiet kind of wrong this tool exists to avoid. The pair is
    exact and documented, so translating is safe where guessing at a renamed *key* would not be.
    """
    out = tmp_path / "out"
    out.mkdir()
    convert_plugin(write(tmp_path, "plugin_spree.ini", CLASSIC_SPREE), out)

    loaded = yaml.safe_load((out / "plugin_spree.yaml").read_text(encoding="utf-8"))
    assert loaded["killing_sprees"][5] == (
        "{player} is on a killing spree # {player} stopped the spree of {victim}"
    )
    assert loaded["losing_sprees"][7].startswith("Keep it up {player}")


def test_the_converted_spree_tables_parse_in_the_plugin_that_reads_them(tmp_path) -> None:  # noqa: ANN001
    """The real parser, not a guess at it: a config that converts but will not load is not a win."""
    from b3.plugins.spree import parse_spree_messages

    out = tmp_path / "out"
    out.mkdir()
    convert_plugin(write(tmp_path, "plugin_spree.ini", CLASSIC_SPREE), out)

    loaded = yaml.safe_load((out / "plugin_spree.yaml").read_text(encoding="utf-8"))
    parsed = parse_spree_messages(loaded["killing_sprees"], "killing_sprees")
    assert parsed[5] == ("{player} is on a killing spree", "{player} stopped the spree of {victim}")


def test_custom_commands_keep_their_level_which_is_a_key_now(tmp_path) -> None:  # noqa: ANN001
    """`[guest commands]` -> `commands: {guest: {...}}`: the level left the section name.

    These are the operator's own commands, so there is nothing to check them against and everything
    to lose by dropping them — and eight sections named `<level> commands` match no section this
    plugin has, so the section-by-section check would have discarded every one.
    """
    from b3.plugins.customcommands import level_for

    out = tmp_path / "out"
    out.mkdir()
    src = write(tmp_path, "plugin_customcommands.ini", CLASSIC_CUSTOMCOMMANDS)
    convert_plugin(src, out)

    loaded = yaml.safe_load((out / "plugin_customcommands.yaml").read_text(encoding="utf-8"))
    assert loaded["commands"]["guest"] == {
        "cookie": "tell <ARG:FIND_PLAYER:PID> have a cookie",
        "ns": "tell <LAST_KILLER:PID> nice shot !",
    }
    assert loaded["commands"]["admin"] == {"slap": "slap <ARG:FIND_PLAYER:PID>"}
    assert loaded["help"] == {"cookie": "give a cookie to a player"}
    # the group keywords the classic put in section names are levels this plugin resolves
    assert level_for("guest") == 0
    assert level_for("admin") is not None


def test_a_plugin_that_takes_no_config_file_is_skipped_with_what_replaced_it(tmp_path) -> None:  # noqa: ANN001
    """`cmdmanager` is still a plugin; its *config file* is what is gone.

    Its levels live in the plugin's own tables now, set with `!cmdlevel`. Converting the file would
    write a `commands:` block nothing reads — a config that looks complete and does nothing, which
    is worse than being told where the setting went.
    """
    out = tmp_path / "out"
    out.mkdir()
    ini = "[settings]\nupdate_config_file: yes\n[commands]\ncmdlevel: superadmin\n"
    result = convert_plugin(write(tmp_path, "plugin_cmdmanager.ini", ini), out)

    assert result.target is None
    assert "!cmdlevel" in result.skipped
    assert not list(out.glob("*.yaml"))


def test_a_plugin_that_is_not_bundled_here_says_so_rather_than_guessing(tmp_path) -> None:  # noqa: ANN001
    """ "declares no settings" and "is not here at all" send you to two different documents."""
    out = tmp_path / "out"
    out.mkdir()
    result = convert_plugin(write(tmp_path, "plugin_chatlogger.ini", "[settings]\na: b\n"), out)

    assert any("is not a plugin bundled here" in note for note in result.notes)
