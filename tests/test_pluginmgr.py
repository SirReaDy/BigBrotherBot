"""Config-driven plugin loader: resolution, dependency order, and disable semantics."""

from __future__ import annotations

import pytest

from b3.config.schema import Config, PluginEntry, ServerConfig
from b3.core.commands import CommandProcessor, command
from b3.core.plugin import Plugin
from b3.core.pluginmgr import (
    LoadedPlugin,
    PluginLoadError,
    load_plugins,
    order_names,
    resolve_plugin_class,
)
from b3.domain.client import Client

# Plugins used as fixtures. They live in this module, so entries reference them as
# "<this module>:ClassName" — the loader accepts an explicit class path for exactly this reason.
HERE = __name__


class AlphaPlugin(Plugin):
    def __init__(self, console, config=None) -> None:  # noqa: ANN001
        super().__init__(console, config)
        self.startups = 0

    def on_startup(self) -> None:
        self.startups += 1

    @command(level=0)
    def cmd_alpha(self, ctx) -> None:  # noqa: ANN001
        """alpha - test command"""
        ctx.reply("alpha")


class BetaPlugin(Plugin):
    """Hard-depends on alpha."""

    requires_plugins = ("alpha",)


class GammaPlugin(Plugin):
    """Soft-orders after beta; works only on cod4."""

    load_after = ("beta",)
    requires_parsers = ("cod4",)


class CyclePlugin(Plugin):
    load_after = ("other",)


class OtherPlugin(Plugin):
    load_after = ("cycle",)


def _entry(name: str, klass: type[Plugin] | None = None, **kw) -> PluginEntry:
    module = f"{HERE}:{klass.__name__}" if klass is not None else None
    return PluginEntry(name=name, module=module, **kw)


def _config(*entries: PluginEntry, game: str = "cod4") -> Config:
    return Config(server=ServerConfig(game=game), plugins=list(entries))


def _by_name(loaded: list[LoadedPlugin]) -> dict[str, LoadedPlugin]:
    return {item.name: item for item in loaded}


# -- class resolution ------------------------------------------------------


def test_resolves_bundled_plugin_by_name_alone():
    from b3.plugins.admin import AdminPlugin

    assert resolve_plugin_class(PluginEntry(name="admin")) is AdminPlugin


def test_resolves_explicit_module_and_class():
    assert resolve_plugin_class(_entry("alpha", AlphaPlugin)) is AlphaPlugin


def test_unknown_plugin_name_is_fatal():
    with pytest.raises(PluginLoadError, match="cannot import"):
        resolve_plugin_class(PluginEntry(name="nope_does_not_exist"))


def test_module_without_a_plugin_class_is_fatal():
    with pytest.raises(PluginLoadError, match="no Plugin subclass"):
        resolve_plugin_class(PluginEntry(name="x", module="b3.core.clock"))


def test_ambiguous_module_asks_for_an_explicit_class(console):
    # This test module defines several Plugin subclasses, so bare-module resolution must refuse.
    with pytest.raises(PluginLoadError, match="several Plugin subclasses"):
        resolve_plugin_class(PluginEntry(name="x", module=HERE))


def test_named_attribute_that_is_not_a_plugin_is_fatal():
    with pytest.raises(PluginLoadError, match="not a Plugin subclass"):
        resolve_plugin_class(PluginEntry(name="x", module=f"{HERE}:HERE"))


# -- ordering --------------------------------------------------------------


def test_requires_and_load_after_order_the_load():
    classes = {"gamma": GammaPlugin, "beta": BetaPlugin, "alpha": AlphaPlugin}
    # Config order is deliberately reversed against the dependency order.
    assert order_names(classes, ["gamma", "beta", "alpha"]) == ["alpha", "beta", "gamma"]


def test_independent_plugins_keep_config_order():
    classes = {"one": AlphaPlugin, "two": AlphaPlugin, "three": AlphaPlugin}
    assert order_names(classes, ["one", "two", "three"]) == ["one", "two", "three"]


def test_dependency_cycle_is_fatal():
    with pytest.raises(PluginLoadError, match="cycle among: cycle, other"):
        order_names({"cycle": CyclePlugin, "other": OtherPlugin}, ["cycle", "other"])


def test_unconfigured_load_after_is_ignored():
    # gamma wants to load after beta, but beta is not configured at all.
    assert order_names({"gamma": GammaPlugin}, ["gamma"]) == ["gamma"]


# -- loading ---------------------------------------------------------------


def test_loads_and_starts_configured_plugins(console):
    loaded = load_plugins(console, _config(_entry("alpha", AlphaPlugin)))

    assert [item.name for item in loaded] == ["alpha"]
    plugin = loaded[0].plugin
    assert loaded[0].enabled is True
    # The loader instantiates but does not start; the runtime does that.
    assert plugin.is_started() is False
    plugin.start()
    assert console.command_registry.get("alpha") is not None


def test_load_order_is_dependency_order(console):
    loaded = load_plugins(
        console,
        _config(
            _entry("gamma", GammaPlugin),
            _entry("beta", BetaPlugin),
            _entry("alpha", AlphaPlugin),
        ),
    )
    assert [item.name for item in loaded] == ["alpha", "beta", "gamma"]


def test_duplicate_entry_is_fatal(console):
    with pytest.raises(PluginLoadError, match="listed twice"):
        load_plugins(console, _config(_entry("alpha", AlphaPlugin), _entry("alpha", AlphaPlugin)))


def test_missing_hard_dependency_is_fatal(console):
    with pytest.raises(PluginLoadError, match="requires plugin 'alpha'"):
        load_plugins(console, _config(_entry("beta", BetaPlugin)))


def test_no_plugins_configured_loads_nothing(console):
    assert load_plugins(console, _config()) == []


# -- disable semantics -----------------------------------------------------


def test_disabled_entry_is_loaded_but_inert(console):
    loaded = _by_name(load_plugins(console, _config(_entry("alpha", AlphaPlugin, disabled=True))))

    item = loaded["alpha"]
    assert item.enabled is False
    assert item.reason == "disabled in config"
    assert item.plugin.is_enabled() is False
    assert item.plugin.disabled_reason == "disabled in config"


def test_disabled_dependency_cascades(console):
    loaded = _by_name(
        load_plugins(
            console,
            _config(_entry("alpha", AlphaPlugin, disabled=True), _entry("beta", BetaPlugin)),
        )
    )
    assert loaded["beta"].enabled is False
    assert loaded["beta"].reason == "required plugin(s) not enabled: alpha"


def test_parser_mismatch_disables_the_plugin(console):
    loaded = _by_name(load_plugins(console, _config(_entry("gamma", GammaPlugin), game="cod7")))
    assert loaded["gamma"].enabled is False
    assert "does not support the 'cod7' parser" in loaded["gamma"].reason


def test_enabling_at_runtime_runs_the_deferred_startup(console):
    loaded = load_plugins(console, _config(_entry("alpha", AlphaPlugin, disabled=True)))
    plugin = loaded[0].plugin

    assert plugin.is_started() is False
    assert console.command_registry.get("alpha") is None

    plugin.enable()

    assert plugin.is_enabled() is True
    assert plugin.startups == 1
    assert plugin.disabled_reason == ""
    assert console.command_registry.get("alpha") is not None


def test_start_is_idempotent(console):
    plugin = AlphaPlugin(console)
    plugin.start()
    plugin.start()
    assert plugin.startups == 1


@pytest.mark.asyncio
async def test_disabled_plugin_commands_are_not_dispatched(console):
    plugin = AlphaPlugin(console)
    plugin.start()
    client = Client(cid="1", guid="g", name="Bob")

    processor = CommandProcessor(console.command_registry, console)
    await processor.handle(client, "!alpha")
    assert console.told[-1][1] == "alpha"

    plugin.disable()
    await processor.handle(client, "!alpha")
    assert console.told[-1][1] == "command is currently disabled: alpha"
    # ...and it drops out of !help too.
    assert console.command_registry.usable_by(client) == []


# -- plugin config files ---------------------------------------------------


def test_plugin_config_file_is_loaded(console, tmp_path):
    (tmp_path / "alpha.yaml").write_text("greeting: hi\n", encoding="utf-8")
    loaded = load_plugins(
        console,
        _config(_entry("alpha", AlphaPlugin, config="@conf/alpha.yaml")),
        conf_dir=tmp_path,
    )
    assert loaded[0].plugin.config == {"greeting": "hi"}


def test_missing_plugin_config_is_fatal_for_an_enabled_plugin(console, tmp_path):
    with pytest.raises(PluginLoadError, match="config file not found"):
        load_plugins(
            console,
            _config(_entry("alpha", AlphaPlugin, config="@conf/absent.yaml")),
            conf_dir=tmp_path,
        )


def test_missing_plugin_config_is_tolerated_for_a_disabled_plugin(console, tmp_path):
    loaded = load_plugins(
        console,
        _config(_entry("alpha", AlphaPlugin, config="@conf/absent.yaml", disabled=True)),
        conf_dir=tmp_path,
    )
    assert loaded[0].plugin.config is None


# -- what an external plugin needs from the API ---------------------------------------------


def test_a_plugin_can_reach_the_storage_engine_to_own_its_tables(tmp_path):
    """The sanctioned replacement for the classic `console.storage.query()` raw-SQL seam."""
    from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select

    from b3.storage.store import SqlAlchemyStorage

    # A plugin's own metadata, kept well away from the core's Alembic-managed schema.
    metadata = MetaData()
    notes = Table(
        "plugin_notes",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("text", String(32)),
    )

    storage = SqlAlchemyStorage(f"sqlite:///{tmp_path / 'b3.sqlite'}")
    storage.connect()

    class Console:
        pass

    console = Console()
    console.storage = storage
    plugin = Plugin.__new__(Plugin)  # no lifecycle needed for this check
    plugin.console = console

    engine = plugin.storage_engine()
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(notes).values(text="hello"))
    with engine.connect() as conn:
        assert [r.text for r in conn.execute(select(notes))] == ["hello"]

    # ...and the core's own tables are untouched next to it.
    assert storage.count_clients() == 0
    storage.close()


def test_storage_without_an_engine_says_so_clearly():
    class Console:
        storage = object()  # a backend that shares nothing

    plugin = Plugin.__new__(Plugin)
    plugin.console = Console()
    with pytest.raises(RuntimeError, match="no SQLAlchemy engine"):
        plugin.storage_engine()


# -- settings that name another plugin -------------------------------------


class NeedsFriendPlugin(Plugin):
    """A plugin whose *setting* — not its class — depends on another plugin being loaded.

    The distinction is the point of `check_config`: `requires_plugins` would force `friend` on every
    operator, when what actually needs it is one setting that most of them will never switch on.
    """

    @classmethod
    def check_config(cls, config: object, configured: frozenset[str]) -> list[str]:
        settings = config.get("settings", {}) if isinstance(config, dict) else {}
        if settings.get("needs_friend") and "friend" not in configured:
            return ["settings.needs_friend is on, but `friend` is not loaded"]
        return []


def _needy(tmp_path, on: bool = True) -> PluginEntry:  # noqa: ANN001
    """The needy plugin, with a real config file — the whole chain, not a stubbed config."""
    path = tmp_path / "plugin_needy.yaml"
    path.write_text(f"settings:\n  needs_friend: {'yes' if on else 'no'}\n", encoding="utf-8")
    return _entry("needy", NeedsFriendPlugin, config=str(path))


def test_a_setting_that_names_a_missing_plugin_refuses_to_start(console, tmp_path):
    """A quiet no-op is the one failure an operator cannot see from in the game."""
    with pytest.raises(PluginLoadError) as raised:
        load_plugins(console, _config(_needy(tmp_path)))

    assert "friend" in str(raised.value) and "needy" in str(raised.value)


def test_the_same_setting_is_fine_once_the_plugin_it_names_is_there(console, tmp_path):
    loaded = load_plugins(console, _config(_needy(tmp_path), _entry("friend", AlphaPlugin)))
    assert _by_name(loaded)["needy"].enabled


def test_the_setting_switched_off_needs_nothing(console, tmp_path):
    loaded = load_plugins(console, _config(_needy(tmp_path, on=False)))
    assert _by_name(loaded)["needy"].enabled


def test_a_plugin_switched_off_in_the_config_cannot_be_depended_on(console, tmp_path):
    """`disabled: true` means it is not there, which is the whole point of the setting."""
    with pytest.raises(PluginLoadError):
        load_plugins(
            console, _config(_needy(tmp_path), _entry("friend", AlphaPlugin, disabled=True))
        )
