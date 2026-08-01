"""Config-driven plugin loading.

The classic bot enabled plugins with XML ``<plugin name="x" config="@b3/conf/plugin_x.ini"/>``
entries, found the class through a ``b3.plugins.<name>.<Name>Plugin`` naming convention, and
resolved the ``requires*`` manifest inside ``Parser.loadPlugins``. This module keeps the good half
of that — a declarative list in the main config, loaded in dependency order — on top of the typed
:class:`b3.config.schema.PluginEntry`, and drops the string conventions:

* ``name`` is the plugin's *identity*: what ``requires_plugins`` / ``load_after`` refer to.
  ``module`` optionally says where the code lives (``pkg.mod`` or ``pkg.mod:ClassName``) — which is
  what a git-installed third-party plugin will set. Unset, it defaults to ``b3.plugins.<name>``.
* load order is a topological sort over ``requires_plugins`` (hard) and ``load_after`` (soft),
  tie-broken by config order so the sequence is deterministic.
* ``disabled: true`` — and an unsatisfiable requirement, and a parser mismatch — *loads* the plugin
  but leaves it inert: never started, no commands registered, no event handlers. It is not dropped,
  so it can be enabled later at runtime (:meth:`b3.core.plugin.Plugin.enable` runs the deferred
  startup). Only an operator mistake — an unknown name, a missing hard dependency, a cycle — is
  fatal, because silently running without a plugin you asked for is worse than refusing to start.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from b3.config.loader import resolve_path_token
from b3.config.schema import Config, PluginEntry
from b3.core.console import Console
from b3.core.plugin import Plugin

log = logging.getLogger(__name__)

# Where a plugin with no explicit ``module`` is looked up.
BUILTIN_PACKAGE = "b3.plugins"


class PluginLoadError(Exception):
    """A plugin configuration error the operator has to fix (bad name, missing dep, cycle)."""


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    """One instantiated plugin plus the decision the loader made about it."""

    name: str
    plugin: Plugin
    enabled: bool
    reason: str = ""  # why it is not enabled (empty when it is)


def resolve_plugin_class(entry: PluginEntry) -> type[Plugin]:
    """Import ``entry``'s module and return its :class:`Plugin` subclass.

    ``module`` may name a class explicitly (``pkg.mod:ClassName``); otherwise the module is scanned
    for exactly one Plugin subclass defined within it (a re-export from a submodule counts, so a
    plugin package can keep its class in ``plugin.py`` and export it from ``__init__``).
    """
    target = entry.module or f"{BUILTIN_PACKAGE}.{entry.name}"
    module_name, _, class_name = target.partition(":")

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise PluginLoadError(
            f"plugin {entry.name!r}: cannot import {module_name!r} ({exc})"
        ) from exc

    if class_name:
        klass = getattr(module, class_name, None)
        if klass is None:
            raise PluginLoadError(
                f"plugin {entry.name!r}: {module_name!r} has no attribute {class_name!r}"
            )
        if not (inspect.isclass(klass) and issubclass(klass, Plugin)):
            raise PluginLoadError(f"plugin {entry.name!r}: {target!r} is not a Plugin subclass")
        return klass

    prefix = module.__name__ + "."
    candidates = [
        obj
        for obj in vars(module).values()
        if inspect.isclass(obj)
        and issubclass(obj, Plugin)
        and obj is not Plugin
        and (obj.__module__ == module.__name__ or obj.__module__.startswith(prefix))
    ]
    if not candidates:
        raise PluginLoadError(f"plugin {entry.name!r}: no Plugin subclass found in {module_name!r}")
    if len(candidates) > 1:
        names = ", ".join(sorted(c.__name__ for c in candidates))
        raise PluginLoadError(
            f"plugin {entry.name!r}: {module_name!r} defines several Plugin subclasses ({names}); "
            f"name one explicitly as module: '{module_name}:ClassName'"
        )
    return candidates[0]


def load_plugin_config(
    entry: PluginEntry, conf_dir: Path | None = None
) -> dict[str, object] | None:
    """Load a plugin's own YAML config, expanding ``@b3``/``@conf``/``@home`` path tokens."""
    if not entry.config:
        return None
    path = Path(resolve_path_token(entry.config, conf_dir))
    if not path.is_file():
        raise PluginLoadError(f"plugin {entry.name!r}: config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def order_names(classes: dict[str, type[Plugin]], config_order: list[str]) -> list[str]:
    """Topologically sort plugin names; ties keep config order so loading is reproducible.

    ``requires_plugins`` and ``load_after`` both constrain order. A name that is not configured is
    ignored here (a *missing* hard requirement is reported by :func:`load_plugins`, which can say
    something more useful about it than "cycle").
    """
    rank = {name: i for i, name in enumerate(config_order)}
    pending: dict[str, set[str]] = {
        name: {
            dep
            for dep in (*klass.requires_plugins, *klass.load_after)
            if dep in classes and dep != name
        }
        for name, klass in classes.items()
    }

    ready = sorted((n for n, deps in pending.items() if not deps), key=rank.__getitem__)
    ordered: list[str] = []
    while ready:
        name = ready.pop(0)
        ordered.append(name)
        del pending[name]
        freed = []
        for other, deps in pending.items():
            if name in deps:
                deps.discard(name)
                if not deps:
                    freed.append(other)
        if freed:
            ready.extend(freed)
            ready.sort(key=rank.__getitem__)

    if pending:
        stuck = sorted(pending, key=rank.__getitem__)
        raise PluginLoadError("plugin dependency cycle among: " + ", ".join(stuck))
    return ordered


def load_plugins(
    console: Console, config: Config, *, conf_dir: Path | None = None
) -> list[LoadedPlugin]:
    """Instantiate the configured plugins, in dependency order, deciding each one's start state.

    ``conf_dir`` is the directory of the main config file — the base for ``@conf`` tokens in a
    plugin's ``config`` path.
    """
    entries: list[PluginEntry] = list(config.plugins)
    classes: dict[str, type[Plugin]] = {}
    by_name: dict[str, PluginEntry] = {}
    for entry in entries:
        if entry.name in by_name:
            raise PluginLoadError(f"plugin {entry.name!r} is listed twice in the config")
        by_name[entry.name] = entry
        classes[entry.name] = resolve_plugin_class(entry)

    for name, klass in classes.items():
        for dep in klass.requires_plugins:
            if dep not in classes:
                raise PluginLoadError(
                    f"plugin {name!r} requires plugin {dep!r}, which is not in the config"
                )

    game = config.server.game
    loaded: list[LoadedPlugin] = []
    enabled_so_far: dict[str, bool] = {}

    for name in order_names(classes, [e.name for e in entries]):
        entry, klass = by_name[name], classes[name]
        reason = _disable_reason(klass, entry, game, enabled_so_far)
        enabled = not reason

        try:
            plugin_config = load_plugin_config(entry, conf_dir)
        except PluginLoadError:
            # A plugin that will not run should not be able to break startup over its config file.
            if enabled:
                raise
            log.warning("plugin %r: config file missing (not loading it; plugin is off)", name)
            plugin_config = None

        plugin = klass(console, plugin_config)
        if not enabled:
            plugin.mark_disabled(reason)
            log.warning("plugin %r loaded but not enabled: %s", name, reason)
        else:
            log.info("plugin %r loaded", name)

        enabled_so_far[name] = enabled
        loaded.append(LoadedPlugin(name=name, plugin=plugin, enabled=enabled, reason=reason))

    return loaded


def _disable_reason(
    klass: type[Plugin], entry: PluginEntry, game: str, enabled_so_far: dict[str, bool]
) -> str:
    """Why this plugin must not start, or ``""`` if it may. Dependencies are already decided."""
    if entry.disabled:
        return "disabled in config"
    parsers = klass.requires_parsers
    if parsers is not None and game not in parsers:
        return f"does not support the {game!r} parser (requires: {', '.join(parsers)})"
    off = [dep for dep in klass.requires_plugins if not enabled_so_far.get(dep, False)]
    if off:
        return "required plugin(s) not enabled: " + ", ".join(off)
    return ""
