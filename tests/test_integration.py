"""End-to-end: replay CoD4 log lines through the whole stack and assert real outcomes."""

from __future__ import annotations

import pytest

from b3.cli import build_bot
from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.clock import FakeClock
from b3.core.events import EventType
from b3.domain.client import PenaltyType
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot

GADMIN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"  # 32-char guids
GBOB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class FakeRcon:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def command(self, cmd: str) -> str:
        self.commands.append(cmd)
        return ""


def _build(tmp_path):
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4"),
    )
    rcon = FakeRcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    bot.add_plugin(AdminPlugin(bot))
    bot.start()
    return bot, rcon


@pytest.mark.asyncio
async def test_full_flow_iamgod_then_ban(tmp_path):
    bot, rcon = _build(tmp_path)

    kills = []
    bot.bus.subscribe(EventType.CLIENT_KILL, lambda e: kills.append(e))
    bans = []
    bot.bus.subscribe(EventType.CLIENT_BAN, lambda e: bans.append(e))

    await bot.replay(
        [
            f"J;{GADMIN};1;Admin",
            f"J;{GBOB};2;Bob",
            f"K;{GBOB};2;allies;Bob;{GADMIN};1;axis;Admin;mp5_mp;100;MOD_RIFLE;chest",
            "say;x;1;Admin;!iamgod",
            "say;x;1;Admin;!permban Bob cheating",
        ]
    )

    # 1. iamgod bootstrapped a superadmin.
    assert bot.storage.has_superadmin() is True

    # 2. The kill event flowed end-to-end (attacker = Admin).
    assert len(kills) == 1
    assert kills[0].client.name == "Admin"
    assert kills[0].target.name == "Bob"

    # 3. The ban was issued over RCON with Bob's slot id.
    assert "banclient 2" in rcon.commands

    # 4. A BAN penalty was persisted against Bob.
    bob = bot.storage.get_client_by_guid(GBOB)
    assert bob is not None
    penalties = bot.storage.get_active_penalties(bob.id, PenaltyType.BAN)
    assert len(penalties) == 1
    assert penalties[0].reason == "cheating"

    # 5. A CLIENT_BAN event was published.
    assert len(bans) == 1


@pytest.mark.asyncio
async def test_config_driven_plugin_list_drives_the_bot(tmp_path):
    """build_bot loads what the config lists — no hardcoded plugin."""
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4"),
        plugins=[PluginEntry(name="admin")],
    )
    bot = build_bot(config, rcon=FakeRcon())

    assert isinstance(bot.get_plugin("admin"), AdminPlugin)
    await bot.replay([f"J;{GADMIN};1;Admin", "say;x;1;Admin;!iamgod"])
    assert bot.storage.has_superadmin() is True


@pytest.mark.asyncio
async def test_disabled_plugin_does_not_serve_its_commands(tmp_path):
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4"),
        plugins=[PluginEntry(name="admin", disabled=True)],
    )
    rcon = FakeRcon()
    bot = build_bot(config, rcon=rcon)

    admin = bot.get_plugin("admin")
    assert admin.is_enabled() is False
    assert admin.is_started() is False

    await bot.replay([f"J;{GADMIN};1;Admin", "say;x;1;Admin;!iamgod"])
    assert bot.storage.has_superadmin() is False  # the command was never registered


@pytest.mark.asyncio
async def test_ban_is_recorded_even_when_rcon_is_down(tmp_path):
    """A dead RCON must not lose the penalty — found running against a remotely-tailed log.

    Bot and game server are separate machines in that setup, so the UDP hop can fail on its own.
    The record is the source of truth; `enforce_ban` re-applies it when the player reconnects.
    """
    bot, rcon = _build(tmp_path)

    def dead(cmd: str) -> str:
        raise OSError("rcon unreachable")

    bans = []
    bot.bus.subscribe(EventType.CLIENT_BAN, lambda e: bans.append(e))
    await bot.replay([f"J;{GADMIN};1;Admin", f"J;{GBOB};2;Bob", "say;x;1;Admin;!iamgod"])
    rcon.command = dead

    with pytest.raises(OSError):
        bot.ban(bot.clients.get_by_cid("2"), "cheating", admin=bot.clients.get_by_cid("1"))

    bob = bot.storage.get_client_by_guid(GBOB)
    assert len(bot.storage.get_active_penalties(bob.id, PenaltyType.BAN)) == 1
    await bot.bus.drain()
    assert len(bans) == 1  # and the rest of the bot was told about it


@pytest.mark.asyncio
async def test_low_level_player_cannot_ban(tmp_path):
    bot, rcon = _build(tmp_path)
    await bot.replay(
        [
            f"J;{GADMIN};1;Rando",
            f"J;{GBOB};2;Bob",
            "say;x;1;Rando;!ban Bob",  # Rando is just a user (level 0)
        ]
    )
    assert "banclient 2" not in rcon.commands
    bob = bot.storage.get_client_by_guid(GBOB)
    assert bot.storage.get_active_penalties(bob.id) == []


# -- chat output must not wait on the server -------------------------------------------------


class SplitRcon:
    """Records the two paths separately, the way a real client treats them."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.writes: list[str] = []

    def command(self, cmd: str) -> str:
        self.commands.append(cmd)
        return ""

    def write(self, cmd: str) -> None:
        self.writes.append(cmd)


@pytest.mark.asyncio
async def test_chat_is_fire_and_forget_but_penalties_are_not(tmp_path):
    """Where the loop-blocking fix lives: `!rules` used to be twenty round trips on the loop thread.

    A penalty stays on the waiting path deliberately — an admin has to hear that their ban never
    reached the server, and there is one of those for every few hundred chat lines.
    """
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4"),
    )
    rcon = SplitRcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    bot.add_plugin(AdminPlugin(bot))
    bot.start()
    rcon.commands.clear()
    await bot.replay([f"J;{GADMIN};1;Admin", f"J;{GBOB};2;Bob", "say;x;1;Admin;!iamgod"])
    # `!iamgod` already replied over the fire-and-forget path, which is the point; start clean.
    rcon.writes.clear()

    bot.say("everyone hears this")
    bot.tell(bot.clients.get_by_cid("2"), "only Bob hears this")

    assert rcon.writes == [
        "say ^2(b3)^7: everyone hears this",
        "tell 2 ^2(b3)^7: ^8[pm]^7 only Bob hears this",
    ]
    assert rcon.commands == []  # nothing waited on a reply

    bot.ban(bot.clients.get_by_cid("2"), "cheating", admin=bot.clients.get_by_cid("1"))
    assert "banclient 2" in rcon.commands


@pytest.mark.asyncio
async def test_an_rcon_client_without_write_still_works(tmp_path):
    """Every fake in this suite is one of these, and so is any older client."""
    bot, rcon = _build(tmp_path)
    rcon.commands.clear()  # the profile's startup cvars
    bot.say("hello")
    assert rcon.commands == ["say ^2(b3)^7: hello"]


@pytest.mark.asyncio
async def test_a_ban_records_which_server_issued_it(tmp_path):
    """`bot.server_id` reaches the penalty row — the prerequisite for a multi-server dashboard."""
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}", server_id="cod4_2"),
        server=ServerConfig(game="cod4"),
    )
    rcon = FakeRcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    bot.add_plugin(AdminPlugin(bot))
    bot.start()
    await bot.replay(
        [
            f"J;{GADMIN};1;Admin",
            f"J;{GBOB};2;Bob",
            "say;x;1;Admin;!iamgod",
            "say;x;1;Admin;!permban Bob cheating",
        ]
    )

    bob = bot.storage.get_client_by_guid(GBOB)
    (penalty,) = bot.storage.get_active_penalties(bob.id, PenaltyType.BAN)
    assert penalty.server_id == "cod4_2"


@pytest.mark.asyncio
async def test_a_plugin_resolves_conf_while_it_is_starting(tmp_path):
    """`@conf` inside a plugin's own settings has to work while that plugin is starting.

    Found on a live CoD4X server: `banlist` resolves its list files through `console.config_path`,
    which `build_bot` set only *after* it returned — so every plugin started with it unset and
    `@conf/banlist_guids.txt` fell back to the working directory. The bot reported it could not read
    a file sitting beside its own config, and the answer changed with wherever it was started from.
    """
    conf = tmp_path / "instance"
    conf.mkdir()
    (conf / "banlist_guids.txt").write_text("1100012345678901\n", encoding="utf-8")
    (conf / "plugin_banlist.yaml").write_text(
        'lists:\n  - name: community\n    kind: guid\n    file: "@conf/banlist_guids.txt"\n',
        encoding="utf-8",
    )

    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4"),
        plugins=[
            PluginEntry(name="admin"),  # banlist requires it
            PluginEntry(name="banlist", config="@conf/plugin_banlist.yaml"),
        ],
    )
    bot = build_bot(config, rcon=FakeRcon(), conf_dir=conf, config_path=str(conf / "b3.yaml"))

    banlist = bot.get_plugin("banlist")
    assert [str(entry.path) for entry in banlist.lists] == [str(conf / "banlist_guids.txt")]
