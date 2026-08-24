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

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from b3 import __version__
from b3.core.plugininstall import Git, PluginInstallError, _version_lt, version_key

log = logging.getLogger(__name__)

#: Where a token comes from when the repository is private. Deliberately the environment and not the
#: config file: a config gets pasted into a support thread, and this must not travel with it.
TOKEN_ENV = "B3_UPDATE_TOKEN"

#: What a release tag looks like. Anything else on the remote — a branch tag, a `latest`, somebody's
#: experiment — is ignored rather than compared, because `version_key` reads numbers out of any
#: string and would happily rank `release-candidate` against `v2.1.0`.
RELEASE_TAG_RE = re.compile(r"^v?\d+(\.\d+)*([-.+][0-9A-Za-z.-]+)?$")

#: Marks a container, where `pip install` in the running environment is the wrong answer entirely:
#: the image *is* the version.
DOCKER_MARKER = Path("/.dockerenv")


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


def check(remote: str, current: str = __version__, *, git: Git | None = None) -> UpdateInfo:
    """Ask a git remote for its highest release tag. Never raises.

    An empty `remote` means the operator switched the feature off, which is not an error and not a
    reason to log anything.
    """
    if not remote.strip():
        return UpdateInfo(current=current, error="no update remote is configured")
    try:
        tags = (git or Git()).remote_tags(_with_token(remote))
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
    "DOCKER_MARKER",
    "RELEASE_TAG_RE",
    "TOKEN_ENV",
    "UpdateChecker",
    "UpdateInfo",
    "check",
    "in_container",
    "install_command",
    "release_tags",
]
