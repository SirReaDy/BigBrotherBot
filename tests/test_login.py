"""The `login` plugin — an admin proving who they are before their level does anything.

The threat is real on these engines: a player's identity is a guid their own client sends, and some
titles hand out none at all, so a spoofed identity is an admin account. The classic plugin's answer is
the right one — demote on connect, restore on `!login` — and it is kept.

What is tested hardest is the password handling, because the classic's was `md5(password)` with no salt
in a column sized for exactly that. These tests pin the three things that follow: a modern hash is
used for anything new, a classic MD5 digest from an imported database still authenticates, and it is
replaced the first time its owner logs in — so nobody is told their password is gone.
"""

from __future__ import annotations

import hashlib

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.login import (
    ITERATIONS,
    MIN_PASSWORD,
    LoginPlugin,
    hash_password,
    is_legacy_hash,
    verify_password,
)


def _login(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.get_plugin = lambda name: console.plugins.get(name)  # noqa: ARG005
    plugin = LoginPlugin(console, {"settings": settings} if settings else None)
    plugin.start()
    return plugin


def _client(console, name="Su", cid="1", id_=1, bits=128, password=None):  # noqa: ANN001, ANN202
    client = Client(
        guid=name[0].upper() * 4, name=name, cid=cid, id=id_, group_bits=bits, password=password
    )
    console.clients.add(client)
    console.register_client(name.lower(), client)
    console.storage.save_client(client)
    return client


async def _auth(console, client):  # noqa: ANN001, ANN202
    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=client))


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _last(console, client):  # noqa: ANN001, ANN202
    told = [text for who, text in console.told if who is client]
    return told[-1] if told else ""


# -- hashing -------------------------------------------------------------------------------------


def test_a_hash_carries_its_own_algorithm_salt_and_cost():
    """So raising the iteration count later leaves every existing password verifiable instead of
    locking its owner out."""
    stored = hash_password("hunter2")
    scheme, iterations, salt, digest = stored.split("$")

    assert scheme == "pbkdf2_sha256"
    assert int(iterations) == ITERATIONS
    assert len(salt) == 32 and len(digest) == 64
    assert verify_password("hunter2", stored)
    assert not verify_password("hunter3", stored)


def test_two_identical_passwords_hash_differently():
    """A salt per password: without it, two admins with the same password are visibly the same, and
    one rainbow table answers every account on every server."""
    assert hash_password("hunter2") != hash_password("hunter2")


def test_a_hash_at_an_older_iteration_count_still_verifies():
    old = hash_password("hunter2", iterations=1000)

    assert verify_password("hunter2", old)


def test_a_classic_md5_digest_is_recognised_and_checked():
    """An imported classic database is full of these. Refusing to check them would mean telling every
    admin of a migrated server that their password is gone."""
    digest = hashlib.md5(b"hunter2", usedforsecurity=False).hexdigest()

    assert is_legacy_hash(digest)
    assert verify_password("hunter2", digest)
    assert not verify_password("wrong", digest)


def test_a_hash_in_no_format_we_know_is_refused_rather_than_guessed(caplog):
    with caplog.at_level("WARNING"):
        assert verify_password("hunter2", "argon2id$3$somesalt$somehash") is False

    assert "cannot check" in caplog.text


# -- the demotion --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_admin_is_demoted_until_they_log_in(console):
    _login(console, threshold_level=40)
    su = _client(console, password=hash_password("hunter2"))

    await _auth(console, su)

    assert su.max_level() == 2  # a regular, so !help still works
    assert "log in" in _last(console, su)


@pytest.mark.asyncio
async def test_somebody_below_the_threshold_is_left_alone(console):
    _login(console, threshold_level=40)
    mod = _client(console, name="Mod", bits=8)  # level 20

    await _auth(console, mod)

    assert mod.max_level() == 20
    assert _last(console, mod) == ""


@pytest.mark.asyncio
async def test_an_admin_with_no_password_is_told_and_so_is_the_log(console, caplog):
    """Their rights are inactive and nothing else says so — an admin who quietly cannot ban is worth a
    line in the log, not only a message in a console they may not be reading."""
    _login(console, threshold_level=40)
    su = _client(console, password=None)

    with caplog.at_level("WARNING"):
        await _auth(console, su)

    assert su.max_level() == 2
    assert "no password" in _last(console, su)
    assert "no password set" in caplog.text


@pytest.mark.asyncio
async def test_the_reminder_says_to_type_it_in_the_console(console):
    """A password typed in chat is a password every other player has read."""
    _login(console, threshold_level=40)
    su = _client(console, password=hash_password("hunter2"))

    await _auth(console, su)

    assert "/tell 1 !login" in _last(console, su)


# -- logging in ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_right_password_gives_the_rights_back(console):
    _login(console, threshold_level=40)
    su = _client(console, password=hash_password("hunter2"))
    await _auth(console, su)

    await _run(console, su, "!login hunter2")

    assert su.max_level() == 100
    assert "rights are active" in _last(console, su)


@pytest.mark.asyncio
async def test_the_wrong_password_gives_nothing_back(console):
    _login(console, threshold_level=40)
    su = _client(console, password=hash_password("hunter2"))
    await _auth(console, su)

    await _run(console, su, "!login wrong")

    assert su.max_level() == 2
    assert "wrong password" in _last(console, su)


@pytest.mark.asyncio
async def test_three_wrong_passwords_stop_the_guessing(console):
    """The classic accepted guesses as fast as somebody could type them, which against a short
    password is not a password at all."""
    _login(console, threshold_level=40)
    su = _client(console, password=hash_password("hunter2"))
    await _auth(console, su)

    for _ in range(3):
        await _run(console, su, "!login wrong")
    assert "try again in" in _last(console, su)

    # And the right password does not work while the lockout stands.
    await _run(console, su, "!login hunter2")
    assert su.max_level() == 2
    assert "try again in" in _last(console, su)

    console.clock.advance(61)
    await _run(console, su, "!login hunter2")
    assert su.max_level() == 100


@pytest.mark.asyncio
async def test_a_classic_password_is_upgraded_on_the_first_successful_login(console):
    """The one moment the plaintext is in hand is the one chance to replace an unsalted MD5, and the
    player is told nothing: their password has not changed, only how it is kept."""
    _login(console, threshold_level=40)
    digest = hashlib.md5(b"hunter2", usedforsecurity=False).hexdigest()
    su = _client(console, password=digest)
    await _auth(console, su)

    await _run(console, su, "!login hunter2")

    assert su.max_level() == 100
    assert su.password is not None and su.password.startswith("pbkdf2_sha256$")
    assert verify_password("hunter2", su.password)
    assert su in console.storage.saved  # and it was written, not only held in memory


@pytest.mark.asyncio
async def test_logging_in_twice_says_so(console):
    _login(console, threshold_level=40)
    su = _client(console, password=hash_password("hunter2"))
    await _auth(console, su)
    await _run(console, su, "!login hunter2")

    await _run(console, su, "!login hunter2")

    assert "already logged in" in _last(console, su)


@pytest.mark.asyncio
async def test_somebody_who_never_needed_to_log_in_is_told_that(console):
    _login(console, threshold_level=40)
    mod = _client(console, name="Mod", bits=8)
    await _auth(console, mod)

    await _run(console, mod, "!login whatever")

    assert "do not need to log in" in _last(console, mod)


@pytest.mark.asyncio
async def test_login_with_no_password_repeats_the_instructions(console):
    _login(console, threshold_level=40)
    su = _client(console, password=hash_password("hunter2"))
    await _auth(console, su)

    await _run(console, su, "!login")

    assert "in the console" in _last(console, su)


# -- setting passwords ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_admin_can_set_their_own_password(console):
    _login(console)
    su = _client(console)

    await _run(console, su, "!setpassword hunter2")

    assert su.password is not None and verify_password("hunter2", su.password)
    assert "password saved" in _last(console, su)


@pytest.mark.asyncio
async def test_a_password_that_short_is_refused(console):
    _login(console)
    su = _client(console)

    await _run(console, su, "!setpassword abc")

    assert su.password is None
    assert f"least {MIN_PASSWORD}" in _last(console, su)


@pytest.mark.asyncio
async def test_a_superadmin_can_set_somebody_elses(console):
    _login(console)
    su = _client(console)
    mod = _client(console, name="Mod", cid="2", id_=2, bits=8)

    await _run(console, su, "!setpassword hunter2 mod")

    assert mod.password is not None and verify_password("hunter2", mod.password)
    assert "password saved for Mod" in _last(console, su)
    assert "password saved" in _last(console, mod)  # and they are told


@pytest.mark.asyncio
async def test_setting_a_password_only_works_downwards(console):
    """Setting somebody's password is taking their account, so it may only be done downwards — with
    superadmins exempt, because somebody has to be able to fix another superadmin's."""
    _login(console)
    senior = _client(console, name="Senior", bits=64)  # level 80
    other = _client(console, name="Other", cid="2", id_=2, bits=64)

    await _run(console, senior, "!setpassword hunter2 other")

    assert other.password is None
    assert "yourself or somebody below" in _last(console, senior)


def test_the_setpassword_level_comes_from_the_config(console):
    _login(console, password_level=100)

    assert console.command_registry.get("setpassword").min_level == 100


# -- through a real bot --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_real_admin_logs_in_over_rcon(tmp_path):
    """The whole path against a real database: an admin joins, is demoted, types `!login` in chat, and
    gets their level back — with the stored hash upgraded from the classic's MD5 on the way."""
    from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
    from b3.core.clock import FakeClock
    from b3.runtime.bot import Bot

    class Rcon:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def command(self, cmd: str) -> str:
            self.commands.append(cmd)
            return ""

    guid = "s" * 32
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4"),
        plugins=[PluginEntry(name="admin"), PluginEntry(name="login")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = LoginPlugin(bot, {"settings": {"threshold_level": 40}})
    bot.add_plugin(plugin, "login")
    bot.start()
    admin.start()
    plugin.start()

    # An imported classic account: superadmin, with an MD5 password.
    bot.storage.save_client(
        Client(
            guid=guid,
            name="Su",
            group_bits=128,
            password=hashlib.md5(b"hunter2", usedforsecurity=False).hexdigest(),
        )
    )
    rcon.commands.clear()

    await bot.replay([f"J;{guid};1;Su"])
    await bot.bus.drain()
    su = bot.clients.get_by_cid("1")
    assert su.max_level() == 2  # demoted on arrival
    assert any("log in" in c for c in rcon.commands)
    # The *session* is demoted and the row is not: a restart must not leave somebody demoted for good.
    on_disk = bot.storage.get_client_by_guid(guid)
    assert on_disk is not None and on_disk.group_bits == 128

    await bot.replay([f"say;{guid};1;Su;!login hunter2"])
    await bot.bus.drain()

    assert su.max_level() == 100
    stored = bot.storage.get_client_by_guid(guid)
    assert stored is not None and stored.password.startswith("pbkdf2_sha256$")
    bot.storage.close()
