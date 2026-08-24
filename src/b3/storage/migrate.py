"""Programmatic Alembic driver.

Lets the runtime and the CLI evolve a database without the ``alembic`` command line: it points an
Alembic ``Config`` at the packaged migration scripts and sets the URL from the b3 config.

**A fresh install is always right; an upgraded one is the problem.** `SqlAlchemyStorage.connect()`
creates a missing schema and stamps it at head, so a new database matches the code by construction.
A database that already existed does not: the code moves to a revision the database has not applied,
and the only symptom is whatever the new column was for failing oddly. With several bots sharing one
database — which is the deployment this project recommends — one forgotten `b3 db upgrade` affects
every server at once.

So the state is something that can be *asked* for, rather than something an operator has to infer:
:class:`SchemaState` says where the database is, where the code is, and which revisions are missing.
`b3 doctor` reports it, `b3 db upgrade` says what it applied, and the bot refuses to start when it is
behind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)


def stamp(url: str, revision: str = "head") -> None:
    """Mark a database as being at ``revision`` without running migrations.

    Used after creating a fresh schema from the ORM metadata, so subsequent ``upgrade`` calls
    know the starting point.
    """
    command.stamp(alembic_config(url), revision)


def current_revision(url: str) -> str | None:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def head_revision(url: str) -> str | None:
    return ScriptDirectory.from_config(alembic_config(url)).get_current_head()


@dataclass(frozen=True, slots=True)
class SchemaState:
    """Where a database is against the code that expects it.

    `unknown` is its own case rather than being folded into "behind": a database the bot could not
    ask (it is down, the credentials are wrong) must not be reported as needing an upgrade, because
    that sends an operator to run a command that will fail for a different reason.
    """

    current: str | None
    head: str | None
    #: Revisions the database has not applied, oldest first. Empty unless `behind`.
    pending: tuple[str, ...] = ()
    #: The database is at a revision this code does not know — a downgrade, or a shared database that
    #: a newer bot has already upgraded.
    ahead: bool = False
    #: Why the question could not be answered at all.
    error: str = ""
    #: Set when the database has no Alembic stamp: a schema created outside `connect()`.
    unstamped: bool = False

    @property
    def unknown(self) -> bool:
        return bool(self.error)

    @property
    def behind(self) -> bool:
        return bool(self.pending)

    @property
    def current_ok(self) -> bool:
        return not self.unknown and not self.behind and not self.ahead and not self.unstamped

    def describe(self) -> str:
        """One line, for a log or a doctor row."""
        if self.unknown:
            return f"could not be read: {self.error}"
        if self.unstamped:
            return f"has no revision stamp; the code expects {self.head}"
        if self.behind:
            listed = ", ".join(self.pending)
            return f"at {self.current}, code expects {self.head} — missing {listed}"
        if self.ahead:
            return f"at {self.current}, which is newer than this code's {self.head}"
        return f"at {self.current} (current)"


def schema_state(url: str) -> SchemaState:
    """Ask a database where it is. Never raises: an unreachable database is an answer.

    The pending list is walked with Alembic's own `iterate_revisions`, so a branched history is
    handled by the tool that owns the concept rather than by comparing two strings — which is the
    mistake that would make `0010` look older than `0009`.
    """
    try:
        head = head_revision(url)
        current = current_revision(url)
    except Exception as exc:  # noqa: BLE001 - a database we cannot ask is a state, not a crash
        return SchemaState(current=None, head=None, error=str(exc))
    if current is None:
        # No `alembic_version` row at all. A schema `connect()` made is stamped, so this is either an
        # empty database or one built by hand.
        return SchemaState(current=None, head=head, unstamped=head is not None)
    if current == head:
        return SchemaState(current=current, head=head)
    scripts = ScriptDirectory.from_config(alembic_config(url))
    try:
        walk = list(scripts.iterate_revisions(head, current))
    except Exception:  # noqa: BLE001 - a revision this code does not have is exactly the ahead case
        return SchemaState(current=current, head=head, ahead=True)
    pending = tuple(reversed([revision.revision for revision in walk]))
    if not pending:
        return SchemaState(current=current, head=head, ahead=True)
    return SchemaState(current=current, head=head, pending=pending)


@dataclass(frozen=True, slots=True)
class UpgradeResult:
    """What an upgrade actually did, so that "it worked" and "it did nothing" are different lines."""

    before: str | None
    after: str | None
    applied: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def describe(self) -> str:
        if not self.changed:
            return f"already at {self.after} — nothing to apply"
        listed = ", ".join(self.applied) if self.applied else "?"
        return f"{self.before or 'nothing'} -> {self.after}, applying {listed}"


def upgrade_reporting(url: str, revision: str = "head") -> UpgradeResult:
    """Upgrade, and say what changed.

    `command.upgrade` succeeds silently whether it applied five migrations or none, which is
    indistinguishable from doing nothing — and "did I remember to run it?" is the question this whole
    entry exists to answer.
    """
    before = schema_state(url)
    upgrade(url, revision)
    after = current_revision(url)
    return UpgradeResult(before=before.current, after=after, applied=before.pending)
