"""Git-based plugin installation — the third directive from the original B3 developer.

The classic bot had no way to distribute a plugin: the forum that served as the plugin hub shut down,
so third-party plugins ended up vendored into the tree or hand-dropped into ``b3/plugins/``. This
module replaces that with ``b3 plugin install <repo>@<tag>``: clone, read a manifest, seed the
plugin's config, record the exact commit, and add it to the ``plugins:`` list in the main config.

Where this sits relative to :mod:`b3.core.pluginmgr`: *install time* vs *load time*. This module puts
code on disk and writes config; the loader decides what runs and in what order. They meet at two
seams — ``PluginEntry.module``, which is set from the manifest's ``entry_point``, and
``requires_plugins``, which the manifest declares and the loader topologically orders.

Deliberate policies:

* **Pinned by default.** A bare repo URL resolves to the highest semver *tag*. Tracking a branch head
  requires naming the branch explicitly, because "install" should mean a reproducible version.
* **No code execution at install time.** The manifest is data (YAML). Nothing from the repo is
  imported or run by ``install`` — a plugin only ever executes once the bot loads it. There is no
  post-install hook by design.
* **Offline operation.** Installing writes files; a bot already running in another process does not
  see it. Live loading belongs with the runtime enable/disable feature, not here.

A plugin repo declares itself with a ``b3plugin.yaml`` at its root::

    name: chatlogger
    version: 1.2.0
    entry_point: b3_chatlogger:ChatLoggerPlugin   # dotted path, resolved after install
    min_core_version: 2.0.0
    requires_plugins: [admin]
    config_template: conf/plugin_chatlogger.yaml  # optional, copied next to the main config
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from b3 import __version__ as CORE_VERSION
from b3.config.loader import resolve_path_token

log = logging.getLogger(__name__)

MANIFEST_NAME = "b3plugin.yaml"
LOCKFILE_NAME = "installed.yaml"
GITHUB_SHORTHAND = re.compile(r"^[\w.-]+/[\w.-]+$")
# A trailing "@ref" — but not the "@" in scp-style git@host:path.
_REF_TAIL = re.compile(r"^[^/:]+$")


class PluginInstallError(Exception):
    """Anything that stops an install: bad spec, git failure, bad manifest, version conflict."""


class PluginManifest(BaseModel):
    """``b3plugin.yaml`` — what a plugin repo says about itself."""

    name: str = Field(min_length=1)
    version: str = "0.0.0"
    entry_point: str = Field(min_length=1)
    min_core_version: str = "0.0.0"
    requires_plugins: list[str] = Field(default_factory=list)
    config_template: str | None = None
    description: str = ""
    homepage: str = ""


@dataclass(frozen=True, slots=True)
class InstalledPlugin:
    """A lockfile record: exactly what is on disk and where it came from."""

    name: str
    url: str
    ref: str
    commit: str
    version: str
    entry_point: str


# -- version helpers -------------------------------------------------------


def version_key(text: str) -> tuple[int, ...]:
    """Sortable key for a version/tag string. Non-numeric parts are ignored, so ``v1.2.0`` == 1.2.0."""
    parts = re.findall(r"\d+", text)
    return tuple(int(p) for p in parts[:4]) or (0,)


def _version_lt(left: str, right: str) -> bool:
    a, b = version_key(left), version_key(right)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) < b + (0,) * (width - len(b))


# -- spec parsing ----------------------------------------------------------


def parse_spec(spec: str) -> tuple[str, str | None]:
    """Split ``<repo>[@<ref>]`` into a clonable URL and an optional ref.

    Accepts ``owner/repo`` shorthand, a bare ``github.com/...`` host path, a full https URL, an
    scp-style ``git@host:path``, and a local filesystem path (which is how the tests work).
    """
    spec = spec.strip()
    if not spec:
        raise PluginInstallError("empty plugin spec")

    url, ref = spec, None
    head, sep, tail = spec.rpartition("@")
    # "git@github.com:u/r" must not be read as ref "github.com:u/r".
    if sep and head and _REF_TAIL.match(tail):
        url, ref = head, tail

    if GITHUB_SHORTHAND.match(url) and not Path(url).exists():
        url = f"https://github.com/{url}"
    elif url.startswith(("github.com/", "gitlab.com/", "bitbucket.org/")):
        url = f"https://{url}"
    return url, ref


def plugin_name_from_url(url: str) -> str:
    """Best-effort plugin name from a repo URL — a fallback only; the manifest is authoritative.

    Splits on every separator a repo location can use, including the backslashes and drive colon of
    a local Windows path (which is how a repo is referenced in tests and by `--ref` on a clone).
    """
    tail = re.split(r"[/\\:]", url.rstrip("/\\"))[-1]
    tail = tail.removesuffix(".git")
    return re.sub(r"^(b3[-_]?)|([-_]?b3(plugin)?)$", "", tail) or tail


# -- git -------------------------------------------------------------------


class Git:
    """The git commands this module needs. A seam: tests can substitute a fake."""

    def run(self, *args: str, cwd: Path | None = None, timeout: int | None = None) -> str:
        """Run one git command. `timeout` overrides the three minutes a clone is allowed.

        A caller somebody is *waiting on* — the update line a command prints — passes a few seconds
        instead: on an offline machine the difference is a terminal that pauses and one that hangs.
        """
        cmd = ["git", *args]
        log.debug("running %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                check=False,
                timeout=180 if timeout is None else timeout,
            )
        except FileNotFoundError as exc:  # git not installed
            raise PluginInstallError("git is not available on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise PluginInstallError(f"git timed out: {' '.join(cmd)}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise PluginInstallError(
                f"git {args[0]} failed: {detail[-1] if detail else f'exit {proc.returncode}'}"
            )
        return proc.stdout

    def remote_tags(self, url: str, timeout: int | None = None) -> list[str]:
        out = self.run("ls-remote", "--tags", "--refs", url, timeout=timeout)
        return [line.rsplit("/", 1)[-1] for line in out.splitlines() if "refs/tags/" in line]

    def clone(self, url: str, ref: str, dest: Path) -> None:
        self.run("clone", "--depth", "1", "--branch", ref, "--", url, str(dest))

    def checkout_ref(self, repo: Path, url: str, ref: str) -> None:
        self.run("fetch", "--depth", "1", "--tags", "origin", ref, cwd=repo)
        self.run("checkout", "--force", "FETCH_HEAD", cwd=repo)

    def head_commit(self, repo: Path) -> str:
        return self.run("rev-parse", "HEAD", cwd=repo).strip()


def resolve_ref(git: Git, url: str, ref: str | None) -> str:
    """Pin an install: an explicit ref wins, otherwise the highest semver tag."""
    if ref:
        return ref
    tags = git.remote_tags(url)
    if not tags:
        raise PluginInstallError(
            f"{url} publishes no tags; name a branch explicitly (e.g. '{url}@main') to install "
            f"an unpinned version"
        )
    best = tags[0]
    for tag in tags[1:]:
        if _version_lt(best, tag):
            best = tag
    return best


# -- manifest --------------------------------------------------------------


def read_manifest(repo: Path) -> PluginManifest:
    path = repo / MANIFEST_NAME
    if not path.is_file():
        raise PluginInstallError(
            f"{path.name} not found in the repository root — not a B3 plugin, or it predates the "
            f"manifest format"
        )
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    try:
        return PluginManifest.model_validate(raw)
    except ValidationError as exc:
        raise PluginInstallError(f"invalid {MANIFEST_NAME}: {exc}") from exc


def check_core_version(manifest: PluginManifest, core_version: str = CORE_VERSION) -> None:
    if _version_lt(core_version, manifest.min_core_version):
        raise PluginInstallError(
            f"plugin {manifest.name!r} needs B3 >= {manifest.min_core_version}; "
            f"this is {core_version}"
        )


# -- lockfile --------------------------------------------------------------


def lockfile_path(plugins_dir: Path) -> Path:
    return plugins_dir / LOCKFILE_NAME


def read_lockfile(plugins_dir: Path) -> dict[str, InstalledPlugin]:
    path = lockfile_path(plugins_dir)
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    records: dict[str, InstalledPlugin] = {}
    for name, row in (raw.get("plugins") or {}).items():
        try:
            records[name] = InstalledPlugin(
                name=name,
                url=row["url"],
                ref=row["ref"],
                commit=row["commit"],
                version=row.get("version", "0.0.0"),
                entry_point=row["entry_point"],
            )
        except (KeyError, TypeError):
            log.warning("ignoring malformed lockfile entry %r in %s", name, path)
    return records


def write_lockfile(plugins_dir: Path, records: dict[str, InstalledPlugin]) -> None:
    rows = {}
    for name, rec in sorted(records.items()):
        row = asdict(rec)
        row.pop("name")
        rows[name] = row
    path = lockfile_path(plugins_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("# Managed by `b3 plugin` — records the exact commit of each installed plugin.\n")
        yaml.safe_dump({"plugins": rows}, fh, sort_keys=True)


# -- import path -----------------------------------------------------------


def installed_plugins_dir(plugins_dir_setting: str, conf_dir: Path | None = None) -> Path:
    """Resolve the configured plugins directory (``@b3``/``@conf``/``@home`` tokens included)."""
    return Path(resolve_path_token(plugins_dir_setting, conf_dir))


def register_installed_plugins(plugins_dir: Path) -> list[str]:
    """Put each installed plugin's directory on ``sys.path`` so its ``entry_point`` can import.

    Returns the paths added. Warns about two plugins exposing the same top-level module name, since
    whichever is registered first would silently win.
    """
    if not plugins_dir.is_dir():
        return []
    added: list[str] = []
    top_level: dict[str, str] = {}
    for name in sorted(read_lockfile(plugins_dir)):
        repo = plugins_dir / name
        if not repo.is_dir():
            log.warning("plugin %r is in the lockfile but missing from %s", name, plugins_dir)
            continue
        for child in repo.iterdir():
            module = child.stem if child.suffix == ".py" else child.name
            if child.name.startswith((".", "_")) or (child.is_file() and child.suffix != ".py"):
                continue
            if module in top_level and top_level[module] != name:
                log.warning(
                    "plugins %r and %r both provide a top-level %r; the first one wins",
                    top_level[module],
                    name,
                    module,
                )
            top_level.setdefault(module, name)
        path = str(repo)
        if path not in sys.path:
            sys.path.insert(0, path)
        added.append(path)
    return added


# -- operations ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """What ``install`` is about to fetch — shown for confirmation before anything is written.

    There is deliberately no destination here: the *manifest* names the plugin, and that is only
    readable after cloning, so any path guessed from the URL would be a guess shown as fact.
    """

    url: str
    ref: str


def plan_install(
    spec: str, *, plugins_dir: Path | None = None, ref: str | None = None, git: Git | None = None
) -> InstallPlan:
    """Resolve a spec to a concrete (url, pinned ref) without writing anything."""
    git = git or Git()
    url, spec_ref = parse_spec(spec)
    return InstallPlan(url=url, ref=resolve_ref(git, url, ref or spec_ref))


def install(
    spec: str,
    *,
    plugins_dir: Path,
    config_path: Path | None = None,
    conf_dir: Path | None = None,
    ref: str | None = None,
    enable: bool = True,
    disabled: bool = False,
    force: bool = False,
    git: Git | None = None,
) -> InstalledPlugin:
    """Clone, validate, seed config, record, and (optionally) add to the main config's plugin list."""
    git = git or Git()
    plan = plan_install(spec, plugins_dir=plugins_dir, ref=ref, git=git)

    records = read_lockfile(plugins_dir)
    # Clone into a staging dir: the manifest is authoritative for the name, and we only learn it
    # after fetching. Nothing lands in its final place until the manifest checks out.
    staging = plugins_dir / f".staging-{plugin_name_from_url(plan.url)}"
    _rmtree(staging)
    plugins_dir.mkdir(parents=True, exist_ok=True)
    git.clone(plan.url, plan.ref, staging)

    try:
        manifest = read_manifest(staging)
        check_core_version(manifest)
        commit = git.head_commit(staging)

        existing = records.get(manifest.name)
        if existing is not None and not force:
            raise PluginInstallError(
                f"plugin {manifest.name!r} is already installed at {existing.ref} "
                f"({existing.commit[:8]}); use `b3 plugin update {manifest.name}` or --force"
            )
        missing = [
            dep
            for dep in manifest.requires_plugins
            if dep not in records and dep != manifest.name and not _is_bundled(dep)
        ]
        if missing:
            raise PluginInstallError(
                f"plugin {manifest.name!r} requires plugin(s) that are not installed: "
                f"{', '.join(missing)} — install them first"
            )

        target = plugins_dir / manifest.name
        _rmtree(target)
        staging.replace(target)
    except BaseException:
        _rmtree(staging)
        raise

    record = InstalledPlugin(
        name=manifest.name,
        url=plan.url,
        ref=plan.ref,
        commit=commit,
        version=manifest.version,
        entry_point=manifest.entry_point,
    )
    records[manifest.name] = record
    write_lockfile(plugins_dir, records)

    config_ref = _seed_config_template(manifest, target, conf_dir)
    if enable and config_path is not None:
        activate_in_config(
            config_path,
            name=manifest.name,
            module=manifest.entry_point,
            plugin_config=config_ref,
            disabled=disabled,
        )
    log.info("installed plugin %r %s (%s)", record.name, record.version, record.ref)
    return record


def update(
    name: str,
    *,
    plugins_dir: Path,
    ref: str | None = None,
    conf_dir: Path | None = None,
    git: Git | None = None,
) -> InstalledPlugin:
    """Move an installed plugin to a new ref (default: the highest semver tag)."""
    git = git or Git()
    records = read_lockfile(plugins_dir)
    current = records.get(name)
    if current is None:
        raise PluginInstallError(f"plugin {name!r} is not installed")
    repo = plugins_dir / name
    if not repo.is_dir():
        raise PluginInstallError(f"plugin {name!r} is recorded but missing from disk ({repo})")

    resolved = resolve_ref(git, current.url, ref)
    git.checkout_ref(repo, current.url, resolved)
    manifest = read_manifest(repo)
    check_core_version(manifest)

    record = InstalledPlugin(
        name=name,
        url=current.url,
        ref=resolved,
        commit=git.head_commit(repo),
        version=manifest.version,
        entry_point=manifest.entry_point,
    )
    records[name] = record
    write_lockfile(plugins_dir, records)
    _seed_config_template(manifest, repo, conf_dir)
    log.info("updated plugin %r to %s (%s)", name, record.version, record.ref)
    return record


def enable(
    name: str,
    *,
    plugins_dirs: list[Path],
    config_path: Path,
    conf_dir: Path | None = None,
    disabled: bool = False,
) -> InstalledPlugin:
    """Turn on an already-installed plugin for *this* server, without downloading anything.

    This is the shared-pool workflow the classic bot had: the plugin files live in one directory
    several servers can see, and each server's own config decides which of them it actually runs.
    ``plugins_dirs`` is searched in order, so a copy installed for this server alone wins over the
    shared one.
    """
    for directory in plugins_dirs:
        record = read_lockfile(directory).get(name)
        if record is None:
            continue
        repo = directory / name
        plugin_config = None
        if repo.is_dir():
            try:
                plugin_config = _seed_config_template(read_manifest(repo), repo, conf_dir)
            except PluginInstallError:  # a repo without a readable manifest still has an entry
                log.debug("no readable manifest for %r; skipping config template", name)
        activate_in_config(
            config_path,
            name=record.name,
            module=record.entry_point,
            plugin_config=plugin_config,
            disabled=disabled,
        )
        log.info("enabled plugin %r from %s", name, directory)
        return record
    searched = ", ".join(str(d) for d in plugins_dirs)
    raise PluginInstallError(f"plugin {name!r} is not installed in: {searched}")


def disable(name: str, *, config_path: Path) -> bool:
    """Stop running a plugin here, leaving its files in place for other servers."""
    removed = deactivate_in_config(config_path, name)
    if not removed:
        raise PluginInstallError(f"plugin {name!r} is not in {config_path}")
    log.info("disabled plugin %r in %s", name, config_path)
    return removed


def remove(
    name: str, *, plugins_dir: Path, config_path: Path | None = None, keep_files: bool = False
) -> None:
    """Drop a plugin from the lockfile, the main config, and (unless kept) the disk."""
    records = read_lockfile(plugins_dir)
    if name not in records:
        raise PluginInstallError(f"plugin {name!r} is not installed")
    del records[name]
    write_lockfile(plugins_dir, records)
    if not keep_files:
        _rmtree(plugins_dir / name)
    if config_path is not None:
        deactivate_in_config(config_path, name)
    log.info("removed plugin %r", name)


def _is_bundled(name: str) -> bool:
    """A dependency that ships with the core needs no install."""
    from importlib.util import find_spec

    try:
        return find_spec(f"b3.plugins.{name}") is not None
    except (ImportError, ValueError):
        return False


def _seed_config_template(
    manifest: PluginManifest, repo: Path, conf_dir: Path | None
) -> str | None:
    """Copy the plugin's default config next to the main config, never clobbering an existing one.

    Returns the ``@conf``-relative path to reference from the main config, or None.
    """
    if not manifest.config_template or conf_dir is None:
        return None
    source = repo / manifest.config_template
    if not source.is_file():
        log.warning(
            "plugin %r declares config_template %s, which is not in the repo",
            manifest.name,
            manifest.config_template,
        )
        return None
    dest = conf_dir / f"plugin_{manifest.name}{source.suffix or '.yaml'}"
    if dest.exists():
        log.info("keeping existing config %s", dest)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        log.info("seeded config %s", dest)
    return f"@conf/{dest.name}"


def _rmtree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, onerror=_force_remove)
    elif path.exists():
        path.unlink()


def _force_remove(func: Callable[[str], None], path: str, _exc: object) -> None:
    """Git checkouts contain read-only objects on Windows; clear the bit and retry."""
    import os
    import stat

    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        log.debug("could not remove %s", path)


# -- main-config editing ---------------------------------------------------
#
# The operator's YAML is hand-maintained, so it is edited in round-trip mode (ruamel) to preserve
# comments, key order and formatting. A plain load/dump would silently strip every comment.


def _round_trip_yaml() -> Any:
    try:
        from ruamel.yaml import YAML
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise PluginInstallError(
            "editing the config needs ruamel.yaml (pip install 'ruamel.yaml>=0.18')"
        ) from exc
    yml = YAML()
    yml.preserve_quotes = True
    # Match the indented-block-sequence style the example config uses, so a rewritten file does not
    # come back with its list items shifted to column 0.
    yml.indent(mapping=2, sequence=4, offset=2)
    return yml


def activate_in_config(
    config_path: Path,
    *,
    name: str,
    module: str | None = None,
    plugin_config: str | None = None,
    disabled: bool = False,
) -> None:
    """Add (or update) an entry in the main config's ``plugins:`` list, preserving comments."""
    if not config_path.is_file():
        raise PluginInstallError(f"config file not found: {config_path}")
    yml = _round_trip_yaml()
    with config_path.open("r", encoding="utf-8") as fh:
        doc = yml.load(fh) or {}

    entries = doc.get("plugins")
    if entries is None:
        entries = []
        doc["plugins"] = entries

    # A plugin whose entry_point is just the bundled location needs no explicit module.
    if module in (f"b3.plugins.{name}", ""):
        module = None

    row = next((e for e in entries if isinstance(e, dict) and e.get("name") == name), None)
    if row is None:
        row = _new_entry(name)
        entries.append(row)
    if module:
        row["module"] = module
    if plugin_config and "config" not in row:
        row["config"] = plugin_config
    if disabled:
        row["disabled"] = True
    else:
        row.pop("disabled", None)

    with config_path.open("w", encoding="utf-8") as fh:
        yml.dump(doc, fh)


def deactivate_in_config(config_path: Path, name: str) -> bool:
    """Remove a plugin's entry from the main config. Returns whether anything changed."""
    if not config_path.is_file():
        return False
    yml = _round_trip_yaml()
    with config_path.open("r", encoding="utf-8") as fh:
        doc = yml.load(fh) or {}
    entries = doc.get("plugins") or []
    keep = [e for e in entries if not (isinstance(e, dict) and e.get("name") == name)]
    if len(keep) == len(entries):
        return False
    doc["plugins"] = keep
    with config_path.open("w", encoding="utf-8") as fh:
        yml.dump(doc, fh)
    return True


def _new_entry(name: str) -> Any:
    from ruamel.yaml.comments import CommentedMap

    row = CommentedMap()
    row["name"] = name
    return row
