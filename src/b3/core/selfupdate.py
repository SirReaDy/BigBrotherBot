"""Notice that a newer version exists, and install it when asked.

**Say the awkward part first.** This project deleted the classic bot's update check
(`b3/update.py`, which polled `master.bigbrotherbot.net/version.json`) and its self-updater
(`pkg_handler.py`, which rewrote the installation from a remote archive), and the README says so as a
selling point. Rebuilding an update check is not a reversal, but the difference has to be real:

* the classic pointed at a domain the project no longer controlled, so it eventually just failed for
  ever, silently. This points at **a repository we own**, named in the config; unreachable means the
  check says so and nothing else changes.
* the classic checked on **every startup**, unconditionally. This checks once a day at most, caches
  the answer, and `update_check: false` turns it off entirely.
* the classic's updater could **rewrite the installation** because a check found something. Here
  `b3 update` is a command an operator types. A check never installs anything.
* the classic **sent the server's version and address** to a third party. This sends nothing: it reads
  public tags over `git ls-remote` and does not report who is asking.

If any of those four stops being true, this module should be deleted rather than watered down.

**`git ls-remote`, not a REST API.** No project id to configure, no API version to track, identical on
github.com and any self-hosted git, works for a public repo with no token — and it reuses the tag
resolution `b3 plugin install` already has tests for. A REST API would only buy release *notes*, and a
tag name with a link is enough to start with.

The one thing to keep in mind while reading `check()`: **a version that is already newer than the
remote's highest tag is not an update.** Somebody running from a git checkout is ahead of every
release by definition, and telling them to downgrade would be worse than saying nothing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from b3 import __version__
from b3.core.plugininstall import Git, PluginInstallError, _version_lt, version_key

log = logging.getLogger(__name__)

#: Where a token comes from when the repository is private. Deliberately the environment and not the
#: config file: a config gets pasted into a support thread, and this must not travel with it.
TOKEN_ENV = "B3_UPDATE_TOKEN"

#: What a release tag looks like. Anything else on the remote — a branch tag, a `latest`, somebody's
#: experiment — is ignored rather than compared, because `version_key` falls back to reading numbers
#: out of any string and would otherwise rank `release-candidate` against `v2.1.0`.
#:
#: **Numbers only, and nothing after them.** A pre-release is a real tag that somebody meant to push,
#: and `b3 update --to v2.1.0-rc1` installs one on purpose — but it is not something to *offer*. An
#: update line that appears on every command has to name the release, and it would go on naming an
#: rc after the release it was a candidate for had shipped.
RELEASE_TAG_RE = re.compile(r"^v?\d+(\.\d+)*$")

#: Marks a container, where `pip install` in the running environment is the wrong answer entirely:
#: the image *is* the version.
DOCKER_MARKER = Path("/.dockerenv")

#: How stale the remembered answer may get before a *command* asks again. A week, not a day: the bot
#: has its own `bot.update_check_interval` while it runs, and this is the path for a machine where no
#: bot is running at the moment — somebody at a terminal, who does not need to pay for a network
#: round trip more often than that to be told about a release.
NOTICE_INTERVAL = 7 * 24 * 60 * 60.0

#: How long a *command* waits on git before giving up on the question. The bot's own check can afford
#: `Git`'s three minutes because it runs off the loop; a command cannot afford to hang somebody's
#: terminal on an offline machine, and having no answer this time is not a failure.
NOTICE_TIMEOUT = 5

#: Set to anything to keep every command silent about updates, for a shell where the line is noise.
QUIET_ENV = "B3_NO_UPDATE_NOTICE"

#: Where the answer is remembered, so that no ordinary command has to ask. Overridable, which is what
#: the tests use rather than writing to the machine running them.
CACHE_ENV = "B3_UPDATE_CACHE"


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    """What the remote's tags say about the version running here."""

    current: str
    latest: str = ""
    #: The remote could not be read, or has no release tags. Reported, never raised: an unreachable
    #: repository must not stop a bot from moderating a game server.
    error: str = ""
    checked_at: float = 0.0

    @property
    def available(self) -> bool:
        return bool(self.latest) and not self.error and _version_lt(self.current, self.latest)

    @property
    def known(self) -> bool:
        return bool(self.latest) and not self.error

    def describe(self) -> str:
        if self.error:
            return f"could not check for updates: {self.error}"
        if not self.latest:
            return "no releases published yet"
        if self.available:
            return f"{self.latest} is available (running {self.current})"
        if _version_lt(self.latest, self.current):
            return f"running {self.current}, which is newer than the latest release {self.latest}"
        return f"{self.current} is the latest release"


def release_tags(tags: list[str]) -> list[str]:
    """The tags that name a release, highest last."""
    releases = [tag for tag in tags if RELEASE_TAG_RE.match(tag.strip())]
    return sorted(releases, key=version_key)


def check(
    remote: str,
    current: str = __version__,
    *,
    git: Git | None = None,
    timeout: int | None = None,
) -> UpdateInfo:
    """Ask a git remote for its highest release tag. Never raises.

    An empty `remote` means the operator switched the feature off, which is not an error and not a
    reason to log anything. `timeout` is for callers somebody is waiting on — see `cached_check`.
    """
    if not remote.strip():
        return UpdateInfo(current=current, error="no update remote is configured")
    try:
        tags = (git or Git()).remote_tags(_with_token(remote), timeout=timeout)
    except PluginInstallError as exc:
        return UpdateInfo(current=current, error=_redact(str(exc)))
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - defence in depth
        return UpdateInfo(current=current, error=_redact(str(exc)))
    releases = release_tags(tags)
    if not releases:
        return UpdateInfo(current=current)
    return UpdateInfo(current=current, latest=releases[-1].lstrip("v"))


def _with_token(remote: str) -> str:
    """Put a token from the environment into an https URL, for a private repository."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token or not remote.startswith("https://") or "@" in remote.split("//", 1)[1]:
        return remote
    return remote.replace("https://", f"https://{token}@", 1)


def _redact(text: str) -> str:
    """Keep a token out of a log line, the way `logsource.safe_url` keeps a password out of one."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    return text.replace(token, "***") if token else text


def install_command(remote: str, tag: str) -> list[str]:
    """The pip invocation that installs a tag.

    `sys.executable -m pip`, never a bare `pip`: the difference is between updating the bot and
    updating whatever else happens to be first on `PATH`.
    """
    ref = tag if tag.startswith("v") else f"v{tag}"
    return [sys.executable, "-m", "pip", "install", "--upgrade", f"git+{remote}@{ref}"]


def in_container() -> bool:
    """Whether this is running in a container, where updating in place is the wrong operation."""
    if DOCKER_MARKER.exists():
        return True
    # Podman and some Kubernetes runtimes have no /.dockerenv; this file names the runtime instead.
    try:
        return "docker" in Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def cache_path() -> Path:
    """Where the last answer is kept.

    A cache and not config or state: losing it costs one `git ls-remote`, which is why it goes to the
    platform's cache directory and never next to the operator's config.
    """
    override = os.environ.get(CACHE_ENV, "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "b3" / "update.json"


def read_cache(remote: str, current: str = __version__) -> UpdateInfo | None:
    """The remembered answer, or None when there is not one worth using.

    None rather than an empty `UpdateInfo` for every way this can go wrong — absent, unreadable,
    truncated by a full disk, written by a version that stored different keys, or *about a different
    remote* — because all of them mean the same thing to a caller: ask, or say nothing.

    `current` is the version running **now**, never the one that was running when the answer was
    written. Somebody who has just upgraded should stop being told to upgrade without waiting a week
    for the cache to expire.
    """
    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("remote") != remote:
        return None
    try:
        return UpdateInfo(
            current=current,
            latest=str(raw.get("latest", "")),
            error=str(raw.get("error", "")),
            checked_at=float(raw.get("checked_at", 0.0)),
        )
    except (TypeError, ValueError):
        return None


def write_cache(remote: str, info: UpdateInfo) -> None:
    """Remember an answer. Best effort, and silent when it fails.

    A read-only home directory, a full disk, a container with no writable cache — none of those are
    a reason for the command somebody actually ran to fail or to complain.
    """
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "remote": remote,
                    "latest": info.latest,
                    "error": info.error,
                    "checked_at": info.checked_at,
                }
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        log.debug("could not write the update cache: %s", exc)


def cached_check(
    remote: str,
    *,
    current: str = __version__,
    interval: float = NOTICE_INTERVAL,
    now: float | None = None,
    timeout: int | None = NOTICE_TIMEOUT,
    git: Git | None = None,
) -> UpdateInfo:
    """The answer, asking only when what is remembered is older than `interval`.

    This is the whole reason a command can mention a new version without being slow: nearly every
    call reads a file. When it does ask, it is with a short timeout, and the answer is written down
    for the next command whether it is good news or an error — a repository that is unreachable
    stays unreachable for the interval too, rather than being retried by every command.
    """
    moment = time.time() if now is None else now
    remembered = read_cache(remote, current)
    if remembered is not None and moment - remembered.checked_at < interval:
        return remembered
    info = check(remote, current, git=git, timeout=timeout)
    stamped = UpdateInfo(
        current=info.current, latest=info.latest, error=info.error, checked_at=moment
    )
    write_cache(remote, stamped)
    return stamped


def notice(info: UpdateInfo | None) -> str:
    """The one line a command prints when there is a newer release, and "" the rest of the time.

    Only an update is worth a line. Being current is not news, and a check that failed is not the
    business of whoever ran `b3 plugins` — both would train an operator to stop reading this.
    """
    if info is None or not info.available:
        return ""
    return f"b3 {info.latest} is available (running {info.current}) — run `b3 update` to install it"


def notices_wanted() -> bool:
    """Whether this shell wants to hear about updates at all."""
    return not os.environ.get(QUIET_ENV, "").strip()


class UpdateChecker:
    """The check as a *cached* thing, for the bot to hold.

    The classic asked on every startup. This asks at most once per `interval`, keeps the answer, and
    hands the same answer to the log line, `!b3` and `b3 doctor` — so no command ever waits on the
    network to print a version.
    """

    def __init__(self, remote: str, interval: float, *, git: Git | None = None) -> None:
        self.remote = remote
        self.interval = interval
        self._git = git
        self._last: UpdateInfo | None = None

    @property
    def last(self) -> UpdateInfo | None:
        """The most recent answer, without asking. None until something has asked once."""
        return self._last

    def due(self, now: float) -> bool:
        if self._last is None:
            return True
        return now - self._last.checked_at >= self.interval

    def check(self, now: float, current: str = __version__) -> UpdateInfo:
        """Ask if it is time to, otherwise hand back the answer already held."""
        if not self.due(now) and self._last is not None:
            return self._last
        info = check(self.remote, current, git=self._git)
        self._last = UpdateInfo(
            current=info.current, latest=info.latest, error=info.error, checked_at=now
        )
        return self._last


__all__ = [
    "CACHE_ENV",
    "DOCKER_MARKER",
    "NOTICE_INTERVAL",
    "NOTICE_TIMEOUT",
    "QUIET_ENV",
    "RELEASE_TAG_RE",
    "TOKEN_ENV",
    "UpdateChecker",
    "UpdateInfo",
    "cache_path",
    "cached_check",
    "check",
    "in_container",
    "install_command",
    "notice",
    "notices_wanted",
    "read_cache",
    "release_tags",
    "write_cache",
]
