"""Makes an admin prove who they are before their level is any use.

A port of the classic `login` plugin, and the threat it exists for is real: on most of these engines a
player's identity is a guid the client sends, and on some of them the server hands out none at all. So
a stolen or spoofed identity is an admin account, and `!ban` with it is an admin's `!ban`. This plugin
demotes anybody above `threshold_level` to a regular the moment they connect, and gives it back when
they type `!login <password>` — which they do through the game's own private-message console, so the
password is not on a line every other player can read.

Kept from the classic: the demotion on authentication, `!login`, `!setpassword` (for yourself, or for
somebody below you), and the reminder telling an admin what to type.

Changed, and the first is the reason this took a migration:

* **Passwords are hashed with PBKDF2, not with bare MD5.** The classic stored `md5(password)` — no
  salt, no iterations — in a `varchar(32)` column sized for exactly that. A rainbow table answers an
  unsalted MD5, so the hash is now `pbkdf2_sha256$iterations$salt$hash` and migration `0003` widens the
  column to hold it. **An imported classic database still works**: a 32-character hex digest is
  recognised as a legacy hash, verified as one, and replaced with a modern hash the first time its
  owner logs in successfully. Nobody has to be given a new password to keep using theirs.
* **A wrong password is rate-limited.** The classic accepted guesses as fast as a player could type
  them, which against a four-character password is not a password at all. Three wrong answers and this
  one stops listening to that player for a while, and says how long.
* **The demotion is announced to the log, not only to the player.** An admin whose password nobody
  ever set is an admin who quietly has no powers; that is worth a line in the log rather than only a
  message in a game console they may not read.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int, duration_text
from b3.domain.client import Client
from b3.domain.permissions import find_group

log = logging.getLogger(__name__)

#: The modern format: algorithm, iterations, salt and hash, so a stored password says how to check it
#: and a later change of algorithm does not invalidate the ones already written.
SCHEME = "pbkdf2_sha256"
ITERATIONS = 600_000
SALT_BYTES = 16

#: An MD5 hex digest, which is what every password in an imported classic database is.
LEGACY_LENGTH = 32

#: Wrong answers before a player is ignored, and for how long. Chosen so a human who mistyped is not
#: locked out and a script gets nowhere: three tries a minute against even a weak password is hopeless.
MAX_ATTEMPTS = 3
LOCKOUT_SECONDS = 60.0

DEFAULTS: dict[str, object] = {
    # Above this level, a password is required. 40 (admin) is the classic's own default: moderators
    # can moderate without one, and anybody who can ban needs to prove it is them.
    "threshold_level": 40,
    # The level `!setpassword` needs.
    "password_level": 40,
    # What a demoted admin is while they are logged out. `reg` keeps `!help` and the harmless commands
    # working, which is the difference between "logged out" and "broken".
    "logged_out_group": "reg",
}

MESSAGES = {
    "login_needed": "log in to use your admin rights: type {command} in the console",
    "login_no_password": "you have no password set, so your admin rights are inactive — ask a "
    "superadmin to set one with !setpassword",
    "login_done": "logged in — your {group} rights are active",
    "login_already": "you are already logged in",
    "login_not_needed": "you do not need to log in",
    "login_denied": "wrong password",
    "login_locked": "too many wrong passwords — try again in {wait}",
    "login_usage": "type this in the console, not in chat: {command}",
    "login_password_set": "password saved",
    "login_password_set_for": "password saved for {name}",
    "login_password_too_low": "you can only set a password for yourself or somebody below you",
    "login_password_short": "that password is too short — use at least {minimum} characters",
}

#: Refused outright. Four characters is not a password when the alternative is somebody else's `!ban`.
MIN_PASSWORD = 6


def hash_password(password: str, *, iterations: int = ITERATIONS, salt: str = "") -> str:
    """Hash a password for storage: ``pbkdf2_sha256$iterations$salt$hash``.

    Self-describing on purpose. The iteration count and the salt travel with the hash, so raising the
    count later leaves every existing password verifiable instead of locking its owner out.
    """
    used = salt or secrets.token_hex(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), used.encode(), iterations).hex()
    return f"{SCHEME}${iterations}${used}${digest}"


def is_legacy_hash(stored: str) -> bool:
    """Whether this is a classic-B3 MD5 digest rather than a modern hash."""
    return len(stored) == LEGACY_LENGTH and all(c in "0123456789abcdefABCDEF" for c in stored)


def verify_password(password: str, stored: str) -> bool:
    """Check a password against either format, in constant time.

    The legacy branch is what lets an imported database keep working. It is not a *safe* hash — that
    is why it is replaced on the first successful login — but refusing to check it would mean telling
    every admin of a migrated server that their password is gone.
    """
    if not stored or not password:
        return False
    if is_legacy_hash(stored):
        legacy = hashlib.md5(password.encode(), usedforsecurity=False).hexdigest()
        return hmac.compare_digest(legacy, stored.lower())
    try:
        scheme, iterations, salt, _digest = stored.split("$", 3)
    except ValueError:
        log.warning("login: a stored password is in no format this bot recognises")
        return False
    if scheme != SCHEME or not iterations.isdigit():
        log.warning("login: a stored password uses %r, which this bot cannot check", scheme)
        return False
    return hmac.compare_digest(
        hash_password(password, iterations=int(iterations), salt=salt), stored
    )


@dataclass
class Session:
    """What this plugin knows about one player's login state."""

    #: The group bits they get back on a successful login. Empty means they never needed to log in.
    restore_bits: int = 0
    logged_in: bool = False
    wrong: int = 0
    locked_until: float = 0.0


class LoginPlugin(Plugin):
    """Demotes admins until they prove who they are."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        registered = self.console.command_registry.get("setpassword")
        if registered is not None:  # pragma: no branch - registered by the framework
            registered.min_level = as_int(self.settings.get("password_level"), 40)

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self.subscribe(EventType.CLIENT_AUTH, self.on_auth)
        registered = self.console.command_registry.get("setpassword")
        if registered is not None:  # pragma: no branch
            registered.min_level = as_int(self.settings.get("password_level"), 40)

    # -- the demotion --------------------------------------------------------

    def session(self, client: Client) -> Session:
        found = client.get_var(self, "login")
        if not isinstance(found, Session):
            found = Session()
            client.set_var(self, "login", found)
        return found

    def on_auth(self, event: Event) -> None:
        """Take an admin's rights away until they log in.

        Their real group bits are remembered on the client — not written anywhere — so a bot restart
        cannot leave somebody demoted in the database. The database row is never changed by this: what
        is lowered is the *session*, which is exactly what the classic did and the only safe version of
        it.
        """
        client = event.client
        if client is None or client.id is None:
            return
        threshold = as_int(self.settings.get("threshold_level"), 40)
        if client.max_level() <= threshold:
            return
        state = self.session(client)
        if state.logged_in:
            return
        state.restore_bits = client.group_bits
        client.group_bits = self._logged_out_bits()
        if not client.password:
            log.warning(
                "login: %s is level %d with no password set, so their rights are inactive until a "
                "superadmin runs !setpassword for them",
                client.name,
                self._level_of(state.restore_bits),
            )
            self.console.tell(client, self.message("login_no_password"))
            return
        self.console.tell(client, self.message("login_needed", command=self._how_to(client)))

    def _logged_out_bits(self) -> int:
        """What a logged-out admin is. `reg` by default: enough to use `!help` and nothing more."""
        keyword = str(self.settings.get("logged_out_group") or "reg")
        group = find_group(keyword)
        if group is None:
            log.error("login: %r is not a group; logging admins out to guest", keyword)
            return 0
        return group.id

    def _level_of(self, bits: int) -> int:
        return Client(group_bits=bits).max_level()

    def _how_to(self, client: Client) -> str:
        """The line to type. In the *console*, privately, because chat is read by everybody.

        The classic picked between `/tell` and `/m` from the game name. Every engine here that has a
        private-message console command spells it one of those two ways, and the bot already knows
        which: `tell_template` is the verb it uses itself.
        """
        verb = "/tell"
        template = getattr(getattr(self.console, "profile", None), "tell_template", "")
        if template.startswith("m ") or " m " in template[:4]:
            verb = "/m"
        return f"{verb} {client.cid} !login <yourpassword>"

    # -- commands ------------------------------------------------------------

    @command(level=2)
    def cmd_login(self, ctx: CommandContext) -> None:
        """login <password> - prove who you are, and get your admin rights back"""
        client = ctx.client
        state = self.session(client)
        if state.logged_in:
            ctx.reply(self.message("login_already"))
            return
        if not state.restore_bits:
            ctx.reply(self.message("login_not_needed"))
            return
        now = self.console.clock.now()
        if state.locked_until > now:
            ctx.reply(
                self.message("login_locked", wait=duration_text((state.locked_until - now) / 60))
            )
            return
        password = ctx.args.strip()
        if not password:
            ctx.reply(self.message("login_usage", command=self._how_to(client)))
            return
        stored = client.password or ""
        if not verify_password(password, stored):
            state.wrong += 1
            if state.wrong >= MAX_ATTEMPTS:
                # Rate-limited, which the classic was not: it accepted guesses as fast as somebody
                # could type them, and a short password does not survive that.
                state.wrong = 0
                state.locked_until = now + LOCKOUT_SECONDS
                log.warning("login: %s has failed %d password attempts", client.name, MAX_ATTEMPTS)
                ctx.reply(self.message("login_locked", wait=duration_text(LOCKOUT_SECONDS / 60)))
                return
            ctx.reply(self.message("login_denied"))
            return

        state.logged_in = True
        state.wrong = 0
        client.group_bits = state.restore_bits
        if is_legacy_hash(stored):
            # The one moment the plaintext is in hand, so it is the one chance to replace an unsalted
            # MD5 with something that cannot be looked up in a table. Silent to the player: their
            # password has not changed, only how it is kept.
            client.password = hash_password(password)
            self.console.storage.save_client(client)
            log.info("login: upgraded %s's stored password from the classic bot's MD5", client.name)
        group = self._group_name(client)
        log.info("login: %s logged in as %s", client.name, group)
        ctx.reply(self.message("login_done", group=group))

    def _group_name(self, client: Client) -> str:
        from b3.domain.permissions import max_group

        group = max_group(client.group_bits)
        return group.name if group is not None else "player"

    @command(level=40)
    def cmd_setpassword(self, ctx: CommandContext) -> None:
        """setpassword <password> [<player>] - set your login password, or somebody else's"""
        parts = ctx.args.split()
        if not parts:
            ctx.reply(self.message("usage", usage="setpassword <password> [<player>]"))
            return
        password = parts[0]
        if len(password) < MIN_PASSWORD:
            ctx.reply(self.message("login_password_short", minimum=MIN_PASSWORD))
            return
        target = ctx.client
        if len(parts) > 1:
            found = self.resolve_client(ctx, parts[1])
            if found is None:
                return
            target = found
            # The classic's rule, and it is the right one: setting somebody's password is taking their
            # account, so it may only be done downwards — with superadmins exempt, since somebody has
            # to be able to fix another superadmin's.
            if ctx.client.max_level() <= target.max_level() and ctx.client.max_level() < 100:
                ctx.reply(self.message("login_password_too_low"))
                return
        target.password = hash_password(password)
        self.console.storage.save_client(target)
        log.info("login: %s set a password for %s", ctx.client.name, target.name)
        if target.cid == ctx.client.cid:
            ctx.reply(self.message("login_password_set"))
            return
        ctx.reply(self.message("login_password_set_for", name=target.name))
        self.console.tell(target, self.message("login_password_set"))


__all__ = [
    "DEFAULTS",
    "MESSAGES",
    "MIN_PASSWORD",
    "LoginPlugin",
    "Session",
    "hash_password",
    "is_legacy_hash",
    "verify_password",
]
