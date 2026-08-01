"""Git-based plugin installation: spec parsing, pinning, manifests, lockfile, config editing.

The install/update/remove tests run against a *real* git repository created in tmp_path, so the
clone/fetch/checkout path is genuinely exercised rather than mocked.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from b3.core.plugininstall import (
    disable,
    enable,
    Git,
    InstalledPlugin,
    PluginInstallError,
    PluginManifest,
    activate_in_config,
    check_core_version,
    deactivate_in_config,
    install,
    installed_plugins_dir,
    parse_spec,
    plan_install,
    plugin_name_from_url,
    read_lockfile,
    read_manifest,
    register_installed_plugins,
    remove,
    resolve_ref,
    update,
    version_key,
    write_lockfile,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# -- spec parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "url", "ref"),
    [
        ("owner/repo", "https://github.com/owner/repo", None),
        ("owner/repo@v1.2.0", "https://github.com/owner/repo", "v1.2.0"),
        ("github.com/o/r@main", "https://github.com/o/r", "main"),
        ("https://github.com/o/r.git", "https://github.com/o/r.git", None),
        ("https://github.com/o/r.git@v2", "https://github.com/o/r.git", "v2"),
        # scp-style: the "@" in git@host must not be read as a ref...
        ("git@github.com:o/r.git", "git@github.com:o/r.git", None),
        # ...but a trailing one still is.
        ("git@github.com:o/r.git@v1.0", "git@github.com:o/r.git", "v1.0"),
        ("gitlab.com/o/r", "https://gitlab.com/o/r", None),
    ],
)
def test_parse_spec(spec, url, ref):
    assert parse_spec(spec) == (url, ref)


def test_parse_spec_rejects_empty():
    with pytest.raises(PluginInstallError, match="empty plugin spec"):
        parse_spec("   ")


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://github.com/o/b3-chatlogger", "chatlogger"),
        ("https://github.com/o/chatlogger.git", "chatlogger"),
        ("https://github.com/o/b3_afk", "afk"),
        ("git@github.com:o/scheduler.git", "scheduler"),
        # A local path, including Windows separators — used for local repos and by the staging dir.
        ("/home/me/repos/b3-afk", "afk"),
        (r"C:\Users\me\repos\src-repo", "src-repo"),
        (r"C:\Users\me\repos\src-repo\\", "src-repo"),
    ],
)
def test_plugin_name_from_url(url, name):
    assert plugin_name_from_url(url) == name


# -- version handling ------------------------------------------------------


def test_version_key_ignores_non_numeric_parts():
    assert version_key("v1.2.3") == version_key("1.2.3") == (1, 2, 3)


def test_highest_semver_tag_wins_not_lexicographic():
    class FakeGit(Git):
        def remote_tags(self, url):  # noqa: ANN001, ANN201
            return ["v1.9.0", "v1.10.0", "v1.2.0"]

    # Lexicographically "v1.9.0" > "v1.10.0"; numerically it is not.
    assert resolve_ref(FakeGit(), "url", None) == "v1.10.0"


def test_explicit_ref_beats_tag_resolution():
    class FakeGit(Git):
        def remote_tags(self, url):  # noqa: ANN001, ANN201
            raise AssertionError("must not query tags when a ref is given")

    assert resolve_ref(FakeGit(), "url", "my-branch") == "my-branch"


def test_untagged_repo_refuses_to_install_unpinned():
    class FakeGit(Git):
        def remote_tags(self, url):  # noqa: ANN001, ANN201
            return []

    with pytest.raises(PluginInstallError, match="publishes no tags"):
        resolve_ref(FakeGit(), "https://example.com/r", None)


def test_core_version_floor_is_enforced():
    manifest = PluginManifest(name="x", entry_point="x:X", min_core_version="3.0.0")
    with pytest.raises(PluginInstallError, match="needs B3 >= 3.0.0"):
        check_core_version(manifest, core_version="2.0.0a0")


def test_core_version_floor_accepts_current_core():
    manifest = PluginManifest(name="x", entry_point="x:X", min_core_version="2.0.0")
    check_core_version(manifest, core_version="2.0.0")


# -- manifests -------------------------------------------------------------


def test_missing_manifest_is_rejected(tmp_path):
    with pytest.raises(PluginInstallError, match="not a B3 plugin"):
        read_manifest(tmp_path)


def test_invalid_manifest_is_rejected(tmp_path):
    (tmp_path / "b3plugin.yaml").write_text("version: 1.0.0\n", encoding="utf-8")
    with pytest.raises(PluginInstallError, match="invalid b3plugin.yaml"):
        read_manifest(tmp_path)


def test_manifest_round_trip(tmp_path):
    (tmp_path / "b3plugin.yaml").write_text(
        "name: demo\nversion: 1.2.3\nentry_point: demo_plugin:DemoPlugin\n", encoding="utf-8"
    )
    manifest = read_manifest(tmp_path)
    assert (manifest.name, manifest.version) == ("demo", "1.2.3")
    assert manifest.entry_point == "demo_plugin:DemoPlugin"


# -- lockfile --------------------------------------------------------------


def test_lockfile_round_trip(tmp_path):
    record = InstalledPlugin(
        name="demo", url="u", ref="v1", commit="c" * 40, version="1.0.0", entry_point="d:D"
    )
    write_lockfile(tmp_path, {"demo": record})
    assert read_lockfile(tmp_path) == {"demo": record}


def test_absent_lockfile_reads_as_empty(tmp_path):
    assert read_lockfile(tmp_path) == {}


def test_malformed_lockfile_entry_is_skipped(tmp_path):
    (tmp_path / "installed.yaml").write_text("plugins:\n  broken:\n    url: u\n", encoding="utf-8")
    assert read_lockfile(tmp_path) == {}


def test_plugins_dir_resolves_path_tokens(tmp_path):
    assert installed_plugins_dir("@conf/plugins", tmp_path) == tmp_path / "plugins"


# -- main-config editing ---------------------------------------------------

CONFIG_WITH_COMMENTS = """\
# Example B3 config.
bot:
  name: b3          # the bot's in-game name
  database: "sqlite:///b3.sqlite"

# Which plugins run.
plugins:
  - name: admin
"""


def _config_file(tmp_path: Path, text: str = CONFIG_WITH_COMMENTS) -> Path:
    path = tmp_path / "b3.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_activate_preserves_comments_and_appends(tmp_path):
    path = _config_file(tmp_path)
    activate_in_config(
        path,
        name="chatlogger",
        module="b3_chatlogger:ChatLoggerPlugin",
        plugin_config="@conf/plugin_chatlogger.yaml",
    )
    text = path.read_text(encoding="utf-8")

    # Comments and the existing entry survive.
    assert "# Example B3 config." in text
    assert "# the bot's in-game name" in text
    assert "# Which plugins run." in text
    # ...including its original indentation: a rewrite must not reflow the operator's file.
    assert "  - name: admin" in text
    # The new entry is there.
    assert "- name: chatlogger" in text
    assert "module: b3_chatlogger:ChatLoggerPlugin" in text
    assert "config: '@conf/plugin_chatlogger.yaml'" in text or 'config: "@conf' in text

    # And it is still valid, loadable config.
    from b3.config.loader import load_config

    names = [p.name for p in load_config(path).plugins]
    assert names == ["admin", "chatlogger"]


def test_activate_is_idempotent(tmp_path):
    path = _config_file(tmp_path)
    activate_in_config(path, name="chatlogger", module="m:C")
    activate_in_config(path, name="chatlogger", module="m:C")

    from b3.config.loader import load_config

    assert [p.name for p in load_config(path).plugins] == ["admin", "chatlogger"]


def test_activate_disabled_then_enabled(tmp_path):
    path = _config_file(tmp_path)
    activate_in_config(path, name="afk", module="m:A", disabled=True)

    from b3.config.loader import load_config

    entry = {p.name: p for p in load_config(path).plugins}["afk"]
    assert entry.disabled is True

    activate_in_config(path, name="afk", module="m:A", disabled=False)
    entry = {p.name: p for p in load_config(path).plugins}["afk"]
    assert entry.disabled is False


def test_activate_omits_module_for_a_bundled_plugin(tmp_path):
    path = _config_file(tmp_path, "plugins: []\n")
    activate_in_config(path, name="admin", module="b3.plugins.admin")
    assert "module" not in path.read_text(encoding="utf-8")


def test_activate_creates_a_missing_plugins_list(tmp_path):
    path = _config_file(tmp_path, "bot:\n  name: b3\n")
    activate_in_config(path, name="admin")

    from b3.config.loader import load_config

    assert [p.name for p in load_config(path).plugins] == ["admin"]


def test_activate_on_missing_config_is_an_error(tmp_path):
    with pytest.raises(PluginInstallError, match="config file not found"):
        activate_in_config(tmp_path / "nope.yaml", name="x")


def test_deactivate_removes_only_the_named_entry(tmp_path):
    path = _config_file(tmp_path)
    activate_in_config(path, name="chatlogger", module="m:C")

    assert deactivate_in_config(path, "chatlogger") is True
    assert deactivate_in_config(path, "chatlogger") is False  # already gone

    from b3.config.loader import load_config

    assert [p.name for p in load_config(path).plugins] == ["admin"]
    assert "# Which plugins run." in path.read_text(encoding="utf-8")


# -- import-path registration ---------------------------------------------


def test_register_installed_plugins_puts_repos_on_sys_path(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    repo = tmp_path / "demo"
    (repo / "demo_plugin").mkdir(parents=True)
    write_lockfile(
        tmp_path,
        {
            "demo": InstalledPlugin(
                name="demo",
                url="u",
                ref="v1",
                commit="c",
                version="1.0.0",
                entry_point="demo_plugin:DemoPlugin",
            )
        },
    )
    added = register_installed_plugins(tmp_path)
    assert added == [str(repo)]
    assert str(repo) in sys.path


def test_register_installed_plugins_ignores_an_empty_dir(tmp_path):
    assert register_installed_plugins(tmp_path / "absent") == []


def test_register_warns_when_two_plugins_collide(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(sys, "path", list(sys.path))
    for name in ("one", "two"):
        (tmp_path / name / "plugin").mkdir(parents=True)
    write_lockfile(
        tmp_path,
        {
            name: InstalledPlugin(
                name=name, url="u", ref="v1", commit="c", version="1.0", entry_point="plugin:P"
            )
            for name in ("one", "two")
        },
    )
    with caplog.at_level("WARNING"):
        register_installed_plugins(tmp_path)
    assert "both provide a top-level 'plugin'" in caplog.text


# -- end-to-end against a real git repository -----------------------------

MANIFEST = """\
name: demo
version: {version}
entry_point: demo_plugin:DemoPlugin
min_core_version: 2.0.0
config_template: conf/demo.yaml
"""

PLUGIN_SOURCE = '''\
"""A minimal installable plugin, used by the installer tests."""

from b3.core.plugin import Plugin


class DemoPlugin(Plugin):
    pass
'''


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def plugin_repo(tmp_path: Path) -> Path:
    """A local git repo holding a valid plugin, tagged v1.0.0 and v1.1.0."""
    repo = tmp_path / "src-repo"
    (repo / "demo_plugin").mkdir(parents=True)
    (repo / "conf").mkdir()
    (repo / "demo_plugin" / "__init__.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    (repo / "conf" / "demo.yaml").write_text("greeting: hello\n", encoding="utf-8")
    (repo / "b3plugin.yaml").write_text(MANIFEST.format(version="1.0.0"), encoding="utf-8")

    _git("init", "-q", "-b", "main", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "v1", cwd=repo)
    _git("tag", "v1.0.0", cwd=repo)

    (repo / "b3plugin.yaml").write_text(MANIFEST.format(version="1.1.0"), encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "v2", cwd=repo)
    _git("tag", "v1.1.0", cwd=repo)
    return repo


@needs_git
def test_plan_install_pins_to_the_highest_tag(tmp_path, plugin_repo):
    plan = plan_install(str(plugin_repo))
    assert plan.ref == "v1.1.0"
    assert plan.url == str(plugin_repo)


@needs_git
def test_install_clones_records_seeds_and_activates(tmp_path, plugin_repo):
    plugins_dir = tmp_path / "plugins"
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    config_path = _config_file(conf_dir)

    record = install(
        str(plugin_repo), plugins_dir=plugins_dir, config_path=config_path, conf_dir=conf_dir
    )

    # 1. pinned to the highest tag, with the real commit recorded
    assert record.ref == "v1.1.0"
    assert record.version == "1.1.0"
    assert len(record.commit) == 40

    # 2. the code landed under the *manifest* name, not the repo name
    assert (plugins_dir / "demo" / "demo_plugin" / "__init__.py").is_file()
    assert not list(plugins_dir.glob(".staging-*"))  # staging cleaned up

    # 3. lockfile
    assert read_lockfile(plugins_dir)["demo"] == record

    # 4. config template seeded next to the main config
    assert (conf_dir / "plugin_demo.yaml").read_text(encoding="utf-8") == "greeting: hello\n"

    # 5. activated in the main config, comments intact
    text = config_path.read_text(encoding="utf-8")
    assert "# Which plugins run." in text
    from b3.config.loader import load_config

    entry = {p.name: p for p in load_config(config_path).plugins}["demo"]
    assert entry.module == "demo_plugin:DemoPlugin"
    assert entry.config == "@conf/plugin_demo.yaml"


@needs_git
def test_installed_plugin_actually_loads(tmp_path, plugin_repo, monkeypatch):
    """The payoff: install, then let the loader resolve and instantiate it."""
    from b3.config.loader import load_config
    from b3.core.pluginmgr import load_plugins

    monkeypatch.setattr(sys, "path", list(sys.path))
    plugins_dir = tmp_path / "plugins"
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    config_path = _config_file(conf_dir, "plugins: []\n")

    install(str(plugin_repo), plugins_dir=plugins_dir, config_path=config_path, conf_dir=conf_dir)
    register_installed_plugins(plugins_dir)

    from tests.conftest import FakeConsole  # noqa: PLC0415

    loaded = load_plugins(FakeConsole(), load_config(config_path), conf_dir=conf_dir)
    assert [item.name for item in loaded] == ["demo"]
    assert type(loaded[0].plugin).__name__ == "DemoPlugin"
    assert loaded[0].enabled is True
    assert loaded[0].plugin.config == {"greeting": "hello"}


@needs_git
def test_install_at_an_explicit_older_tag(tmp_path, plugin_repo):
    record = install(
        f"{plugin_repo}@v1.0.0", plugins_dir=tmp_path / "p", conf_dir=tmp_path, enable=False
    )
    assert (record.ref, record.version) == ("v1.0.0", "1.0.0")


@needs_git
def test_reinstall_is_refused_without_force(tmp_path, plugin_repo):
    plugins_dir = tmp_path / "plugins"
    install(str(plugin_repo), plugins_dir=plugins_dir, conf_dir=tmp_path, enable=False)
    with pytest.raises(PluginInstallError, match="already installed"):
        install(str(plugin_repo), plugins_dir=plugins_dir, conf_dir=tmp_path, enable=False)

    # --force replaces it
    record = install(
        str(plugin_repo), plugins_dir=plugins_dir, conf_dir=tmp_path, enable=False, force=True
    )
    assert record.version == "1.1.0"


@needs_git
def test_no_enable_leaves_the_config_alone(tmp_path, plugin_repo):
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    config_path = _config_file(conf_dir)
    install(
        str(plugin_repo),
        plugins_dir=tmp_path / "plugins",
        config_path=config_path,
        conf_dir=conf_dir,
        enable=False,
    )
    assert config_path.read_text(encoding="utf-8") == CONFIG_WITH_COMMENTS


@needs_git
def test_install_does_not_clobber_an_existing_plugin_config(tmp_path, plugin_repo):
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (conf_dir / "plugin_demo.yaml").write_text("greeting: mine\n", encoding="utf-8")
    install(str(plugin_repo), plugins_dir=tmp_path / "plugins", conf_dir=conf_dir, enable=False)
    assert (conf_dir / "plugin_demo.yaml").read_text(encoding="utf-8") == "greeting: mine\n"


@needs_git
def test_a_repo_without_a_manifest_installs_nothing(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "readme.md").write_text("nothing here\n", encoding="utf-8")
    _git("init", "-q", "-b", "main", cwd=bare)
    _git("add", "-A", cwd=bare)
    _git("commit", "-qm", "init", cwd=bare)
    _git("tag", "v1.0.0", cwd=bare)

    plugins_dir = tmp_path / "plugins"
    with pytest.raises(PluginInstallError, match="not a B3 plugin"):
        install(str(bare), plugins_dir=plugins_dir, conf_dir=tmp_path, enable=False)

    # Nothing is left behind: no staging dir, no lockfile entry.
    assert not list(plugins_dir.glob(".staging-*"))
    assert read_lockfile(plugins_dir) == {}


@needs_git
def test_update_moves_to_a_named_ref_and_back(tmp_path, plugin_repo):
    plugins_dir = tmp_path / "plugins"
    install(f"{plugin_repo}@v1.0.0", plugins_dir=plugins_dir, conf_dir=tmp_path, enable=False)

    updated = update("demo", plugins_dir=plugins_dir, conf_dir=tmp_path)
    assert (updated.ref, updated.version) == ("v1.1.0", "1.1.0")
    assert read_lockfile(plugins_dir)["demo"].version == "1.1.0"

    back = update("demo", plugins_dir=plugins_dir, ref="v1.0.0", conf_dir=tmp_path)
    assert (back.ref, back.version) == ("v1.0.0", "1.0.0")


def test_update_of_an_uninstalled_plugin_is_an_error(tmp_path):
    with pytest.raises(PluginInstallError, match="not installed"):
        update("ghost", plugins_dir=tmp_path)


@needs_git
def test_remove_deletes_files_lockfile_entry_and_config_entry(tmp_path, plugin_repo):
    plugins_dir = tmp_path / "plugins"
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    config_path = _config_file(conf_dir)
    install(str(plugin_repo), plugins_dir=plugins_dir, config_path=config_path, conf_dir=conf_dir)

    remove("demo", plugins_dir=plugins_dir, config_path=config_path)

    assert not (plugins_dir / "demo").exists()
    assert read_lockfile(plugins_dir) == {}
    from b3.config.loader import load_config

    assert [p.name for p in load_config(config_path).plugins] == ["admin"]


@needs_git
def test_remove_can_keep_the_files(tmp_path, plugin_repo):
    plugins_dir = tmp_path / "plugins"
    install(str(plugin_repo), plugins_dir=plugins_dir, conf_dir=tmp_path, enable=False)
    remove("demo", plugins_dir=plugins_dir, keep_files=True)
    assert (plugins_dir / "demo" / "b3plugin.yaml").is_file()
    assert read_lockfile(plugins_dir) == {}


def test_remove_of_an_uninstalled_plugin_is_an_error(tmp_path):
    with pytest.raises(PluginInstallError, match="not installed"):
        remove("ghost", plugins_dir=tmp_path)


@needs_git
def test_a_dependency_that_is_neither_installed_nor_bundled_blocks_install(tmp_path, plugin_repo):
    # Rewrite the manifest to require something that does not exist, and re-tag.
    (plugin_repo / "b3plugin.yaml").write_text(
        textwrap.dedent(
            """\
            name: demo
            version: 2.0.0
            entry_point: demo_plugin:DemoPlugin
            requires_plugins: [admin, nosuchplugin]
            """
        ),
        encoding="utf-8",
    )
    _git("add", "-A", cwd=plugin_repo)
    _git("commit", "-qm", "deps", cwd=plugin_repo)
    _git("tag", "v2.0.0", cwd=plugin_repo)

    with pytest.raises(PluginInstallError, match="nosuchplugin"):
        install(str(plugin_repo), plugins_dir=tmp_path / "p", conf_dir=tmp_path, enable=False)


@needs_git
def test_bundled_dependencies_do_not_need_installing(tmp_path, plugin_repo):
    """`requires_plugins: [admin]` is satisfied by the bundled admin plugin."""
    (plugin_repo / "b3plugin.yaml").write_text(
        textwrap.dedent(
            """\
            name: demo
            version: 3.0.0
            entry_point: demo_plugin:DemoPlugin
            requires_plugins: [admin]
            """
        ),
        encoding="utf-8",
    )
    _git("add", "-A", cwd=plugin_repo)
    _git("commit", "-qm", "deps", cwd=plugin_repo)
    _git("tag", "v3.0.0", cwd=plugin_repo)

    record = install(str(plugin_repo), plugins_dir=tmp_path / "p", conf_dir=tmp_path, enable=False)
    assert record.version == "3.0.0"


@needs_git
def test_core_version_floor_blocks_install(tmp_path, plugin_repo):
    (plugin_repo / "b3plugin.yaml").write_text(
        textwrap.dedent(
            """\
            name: demo
            version: 9.0.0
            entry_point: demo_plugin:DemoPlugin
            min_core_version: 99.0.0
            """
        ),
        encoding="utf-8",
    )
    _git("add", "-A", cwd=plugin_repo)
    _git("commit", "-qm", "floor", cwd=plugin_repo)
    _git("tag", "v9.0.0", cwd=plugin_repo)

    with pytest.raises(PluginInstallError, match="needs B3 >= 99.0.0"):
        install(str(plugin_repo), plugins_dir=tmp_path / "p", conf_dir=tmp_path, enable=False)


def test_git_failure_is_reported_cleanly(tmp_path):
    with pytest.raises(PluginInstallError, match="git clone failed"):
        Git().clone("file:///definitely/not/a/repo", "v1", tmp_path / "dest")


# -- the shared pool: install once, enable per server -------------------------------------------


@needs_git
def test_enable_activates_a_plugin_already_in_the_shared_pool(tmp_path, plugin_repo):
    """Server 1 installs into the pool; server 2 just switches it on — no second download."""
    shared = tmp_path / "shared-plugins"
    conf1, conf2 = tmp_path / "srv1", tmp_path / "srv2"
    conf1.mkdir()
    conf2.mkdir()
    config1, config2 = _config_file(conf1), _config_file(conf2)

    install(str(plugin_repo), plugins_dir=shared, config_path=config1, conf_dir=conf1)

    record = enable(
        "demo",
        plugins_dirs=[conf2 / "plugins", shared],
        config_path=config2,
        conf_dir=conf2,
    )

    assert record.name == "demo"
    text = config2.read_text(encoding="utf-8")
    assert "name: demo" in text
    assert (conf2 / "plugin_demo.yaml").is_file()  # its config template was seeded here too
    assert not (conf2 / "plugins").exists()  # and nothing was downloaded into this server's dir


@needs_git
def test_a_server_specific_copy_is_preferred_over_the_pool(tmp_path, plugin_repo):
    shared = tmp_path / "shared-plugins"
    local = tmp_path / "srv" / "plugins"
    conf = tmp_path / "srv"
    conf.mkdir(parents=True)
    config = _config_file(conf)

    install(str(plugin_repo), plugins_dir=shared, config_path=config, conf_dir=conf)
    install(
        str(plugin_repo),
        plugins_dir=local,
        config_path=config,
        conf_dir=conf,
        ref="v1.0.0",
        enable=False,
    )

    record = enable("demo", plugins_dirs=[local, shared], config_path=config, conf_dir=conf)

    assert record.ref == "v1.0.0"  # this server's pinned copy, not the pool's newer one


def test_enable_reports_a_plugin_that_is_not_installed(tmp_path):
    conf = tmp_path / "srv"
    conf.mkdir()
    config = _config_file(conf)
    with pytest.raises(PluginInstallError, match="not installed"):
        enable("nosuch", plugins_dirs=[tmp_path / "plugins"], config_path=config, conf_dir=conf)


@needs_git
def test_disable_stops_running_it_here_but_leaves_the_files(tmp_path, plugin_repo):
    shared = tmp_path / "shared-plugins"
    conf = tmp_path / "srv"
    conf.mkdir()
    config = _config_file(conf)
    install(str(plugin_repo), plugins_dir=shared, config_path=config, conf_dir=conf)

    disable("demo", config_path=config)

    assert "name: demo" not in config.read_text(encoding="utf-8")
    assert (shared / "demo").is_dir()  # other servers still need them
    assert read_lockfile(shared)["demo"].name == "demo"


def test_disable_reports_a_plugin_that_is_not_in_this_config(tmp_path):
    conf = tmp_path / "srv"
    conf.mkdir()
    config = _config_file(conf)
    with pytest.raises(PluginInstallError, match="not in"):
        disable("nosuch", config_path=config)
