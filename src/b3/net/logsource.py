"""Game-log ingestion.

Tails a growing game-server log and yields complete text lines. Carries over the one genuinely
important operational detail from the legacy ``Parser.read()``: **rotation/truncation detection**
(if the file shrinks, the server rolled or truncated the log, so seek back to the start rather than
sitting past EOF forever).

Bytes-in / str-out: the file is read as bytes and decoded with the configured game encoding
(CoD engines are typically latin-1) using ``errors='replace'`` so a stray non-decodable byte in a
player name never crashes ingestion.

Besides the local :class:`FileLogSource`, this module tails a log over **FTP, SFTP and HTTP** —
the classic ``ftpytail`` / ``sftpytail`` / ``httpytail`` plugins, which is how B3 is normally run
against a hosted game server you have no shell on. Those all resume by byte offset and share their
whole tailing story (offset bookkeeping, rotation detection, partial-line buffering, reconnect
back-off) in :class:`RemoteLogSource`; a protocol only implements *connect / size / read*.

Pick one with :func:`create_log_source`, which dispatches on the URL scheme of ``server.game_log``.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from base64 import b64encode
from pathlib import Path
from typing import Any, BinaryIO, Protocol, runtime_checkable
from urllib.parse import unquote, urlsplit

from b3.core.clock import Clock, SystemClock

log = logging.getLogger(__name__)

#: Schemes handled by :func:`create_log_source`. Anything else is treated as a filesystem path,
#: so a Windows path (``F:\logs\games_mp.log``, scheme ``f``) is never mistaken for a URL.
URL_SCHEMES = ("file", "ftp", "ftps", "sftp", "http", "https")

#: Default poll cadence for remote sources, in seconds. Remote polls cost a round trip (and on FTP
#: a fresh data connection), so they are far slower than the local tailer's tight loop.
DEFAULT_POLL_INTERVAL = 2.0

#: If the log has grown by more than this many bytes since the last successful poll, skip ahead
#: instead of replaying the gap. Guards the first poll against a season's worth of history and a
#: rotation against a full re-read. Legacy called this ``maxGapBytes``. 0 disables the skip.
DEFAULT_MAX_GAP = 20480


class LogSourceError(Exception):
    """A log source could not be built or read in a way the operator must fix."""


@runtime_checkable
class LogSource(Protocol):
    """A source of game-log lines."""

    #: True if :meth:`read_lines` does network I/O and must not run on the event loop thread.
    blocking: bool
    #: Seconds to wait between polls; 0 means "poll as fast as the caller likes" (local files).
    poll_interval: float

    def open(self) -> None: ...
    def read_lines(self) -> list[str]: ...
    def close(self) -> None: ...


def _split_lines(data: bytes, buf: bytes, encoding: str, errors: str) -> tuple[list[str], bytes]:
    """Split ``buf + data`` into complete decoded lines plus the new incomplete-line buffer."""
    parts = (buf + data).split(b"\n")
    # The last element is an incomplete line (no trailing newline yet); keep it buffered.
    tail = parts.pop()
    return [p.rstrip(b"\r").decode(encoding, errors) for p in parts], tail


class FileLogSource:
    """Tail a local log file.

    :param from_start: if True, read the file from its beginning (used by the replay harness and
        tests); if False (default), start at the current end and only report newly-appended lines.
    """

    blocking = False
    poll_interval = 0.0

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        encoding: str = "latin-1",
        errors: str = "replace",
        from_start: bool = False,
    ) -> None:
        self._path = Path(path)
        self._encoding = encoding
        self._errors = errors
        self._from_start = from_start
        self._fh: BinaryIO | None = None
        self._buf = b""  # bytes of an incomplete trailing line

    def open(self) -> None:
        self._fh = open(self._path, "rb")
        if not self._from_start:
            self._fh.seek(0, os.SEEK_END)
        self._buf = b""

    def read_lines(self) -> list[str]:
        if self._fh is None:
            raise RuntimeError("LogSource not opened")

        try:
            size = os.fstat(self._fh.fileno()).st_size
        except OSError:  # pragma: no cover - transient
            return []

        pos = self._fh.tell()
        if size < pos:
            # File shrank: rotated or truncated. Restart from the top of the (new) file.
            log.info("log rotation/truncation detected on %s; seeking to start", self._path)
            self._fh.seek(0)
            self._buf = b""

        data = self._fh.read()
        if not data:
            return []

        lines, self._buf = _split_lines(data, self._buf, self._encoding, self._errors)
        return lines

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class RemoteLogSource(ABC):
    """Byte-offset tailing of a log we can only reach over the network.

    Subclasses implement three small primitives — :meth:`_connect`, :meth:`_remote_size`,
    :meth:`_read_from` and :meth:`_disconnect` — and inherit everything that is actually easy to
    get wrong:

    * **Resume by offset.** We remember how many bytes we have consumed and ask only for what came
      after; the offset survives a reconnect, so a dropped FTP session does not replay the log.
    * **Rotation detection.** If the remote file is now *smaller* than our offset, the server rolled
      or truncated it; restart from byte 0 (same rule as the local tailer).
    * **Gap skipping.** If more than ``max_gap`` bytes appeared since the last poll, jump to the
      last ``max_gap`` bytes and drop the leading partial line rather than flooding the bot with
      history it will happily act on.
    * **Reconnect with back-off.** A network blip must never kill the bot: reads that raise are
      logged, the connection is dropped, and the next attempt is delayed (exponential, capped).
      :meth:`read_lines` returns ``[]`` instead of propagating.

    Credentials come from the URL (``ftp://user:pass@host/path``) and are never logged — log
    :attr:`safe_url`, which is redacted.
    """

    blocking = True

    def __init__(
        self,
        url: str,
        *,
        encoding: str = "latin-1",
        errors: str = "replace",
        from_start: bool = False,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        timeout: float = 10.0,
        max_gap: int = DEFAULT_MAX_GAP,
        retry_interval: float = 5.0,
        retry_max: float = 300.0,
        clock: Clock | None = None,
    ) -> None:
        self.url = url
        self.parts = urlsplit(url)
        if not self.parts.hostname:
            raise LogSourceError(f"no host in game log URL: {self.safe_url}")
        self.path = unquote(self.parts.path) or "/"
        self.poll_interval = poll_interval
        self._encoding = encoding
        self._errors = errors
        self._from_start = from_start
        self._timeout = timeout
        self._max_gap = max_gap
        self._retry_interval = retry_interval
        self._retry_max = retry_max
        self._clock = clock or SystemClock()

        self._offset: int | None = None  # None until the first successful size probe
        self._buf = b""
        self._connected = False
        self._failures = 0
        self._retry_at = 0.0

    # -- operator-facing ------------------------------------------------------------------

    @property
    def safe_url(self) -> str:
        """The URL with any password replaced by ``***`` — safe to log."""
        p = self.parts
        if not p.password:
            return self.url
        host = f"{p.hostname}:{p.port}" if p.port else (p.hostname or "")
        return f"{p.scheme}://{p.username}:***@{host}{p.path}"

    @property
    def username(self) -> str | None:
        return unquote(self.parts.username) if self.parts.username else None

    @property
    def password(self) -> str | None:
        return unquote(self.parts.password) if self.parts.password else None

    # -- LogSource ------------------------------------------------------------------------

    def open(self) -> None:
        """Connect eagerly so a wrong host or password fails loudly at startup.

        A connection that merely *drops* later is handled by the back-off in :meth:`read_lines`;
        this only surfaces the operator's typo while they are still watching the console.
        """
        try:
            self._connect()
        except LogSourceError:
            raise
        except Exception as exc:
            raise LogSourceError(f"cannot open {self.safe_url}: {exc}") from exc
        self._connected = True
        log.info("tailing %s", self.safe_url)

    def read_lines(self) -> list[str]:
        now = self._clock.now()
        if not self._connected:
            if now < self._retry_at:
                return []
            try:
                self._connect()
            except Exception as exc:
                self._fail(exc, now)
                return []
            self._connected = True
            self._failures = 0
            log.info("reconnected to %s", self.safe_url)

        try:
            size = self._remote_size()
            data = self._advance(size)
        except Exception as exc:
            self._fail(exc, now)
            return []

        self._failures = 0
        if not data:
            return []
        lines, self._buf = _split_lines(data, self._buf, self._encoding, self._errors)
        return lines

    def close(self) -> None:
        if self._connected:
            try:
                self._disconnect()
            except Exception as exc:  # pragma: no cover - best effort on shutdown
                log.debug("error closing %s: %s", self.safe_url, exc)
            self._connected = False

    # -- internals ------------------------------------------------------------------------

    def _advance(self, size: int) -> bytes:
        """Work out what to fetch for a log now ``size`` bytes long, and fetch it."""
        if self._offset is None:
            # First poll: tail from the end unless we were asked to replay the whole log.
            self._offset = 0 if self._from_start else size

        if size < self._offset:
            log.info(
                "log rotation/truncation detected on %s (%d < %d); reading from the start",
                self.safe_url,
                size,
                self._offset,
            )
            self._offset = 0
            self._buf = b""

        gap = size - self._offset
        if gap <= 0:
            return b""

        skipped = False
        if self._max_gap and gap > self._max_gap:
            log.warning(
                "%s grew by %d bytes since the last read; skipping to the last %d",
                self.safe_url,
                gap,
                self._max_gap,
            )
            self._offset = size - self._max_gap
            gap = self._max_gap
            self._buf = b""
            skipped = True

        data = self._read_from(self._offset, gap)
        self._offset += len(data)
        if skipped:
            # We landed mid-line; drop the fragment so the parser never sees half an event.
            head, sep, rest = data.partition(b"\n")
            data = rest if sep else b""
        return data

    def _fail(self, exc: Exception, now: float) -> None:
        """Log a failed poll, drop the connection and schedule the next attempt."""
        self._failures += 1
        delay = min(self._retry_max, self._retry_interval * 2 ** (self._failures - 1))
        self._retry_at = now + delay
        log.warning(
            "%s: %s (attempt %d); retrying in %.0fs", self.safe_url, exc, self._failures, delay
        )
        try:
            self._disconnect()
        except Exception:  # pragma: no cover - the connection is already broken
            pass
        self._connected = False

    @abstractmethod
    def _connect(self) -> None:
        """Establish the connection. Raise on failure."""

    @abstractmethod
    def _remote_size(self) -> int:
        """Return the current size of the remote log in bytes."""

    @abstractmethod
    def _read_from(self, offset: int, length: int) -> bytes:
        """Return up to ``length`` bytes starting at ``offset``.

        Returning more than ``length`` is allowed (the file may have grown mid-transfer); the
        caller advances its offset by what it actually got.
        """

    @abstractmethod
    def _disconnect(self) -> None:
        """Tear down the connection. Called on close and after every failed poll."""


class FtpLogSource(RemoteLogSource):
    """Tail over FTP (``ftp://``) or implicit-TLS FTP (``ftps://``) — the legacy ``ftpytail``.

    Resumes with ``REST``, which every FTP server used by a game host supports. The control
    connection is kept open between polls and re-established by the base class if it drops
    (idle timeouts on game-host FTP servers are common and expected).
    """

    _ftp: Any = None

    def _connect(self) -> None:
        from ftplib import FTP, FTP_TLS

        cls = FTP_TLS if self.parts.scheme == "ftps" else FTP
        ftp = cls(timeout=self._timeout)
        ftp.connect(self.parts.hostname or "", self.parts.port or 21)
        ftp.login(self.username or "anonymous", self.password or "")
        if isinstance(ftp, FTP_TLS):
            ftp.prot_p()  # encrypt the data connection too, not just the login
        ftp.set_pasv(True)  # hosted servers are behind NAT; active mode cannot reach us
        ftp.voidcmd("TYPE I")  # SIZE and REST are only meaningful in binary mode
        self._ftp = ftp

    def _remote_size(self) -> int:
        size = self._ftp.size(self.path)
        if size is None:
            raise LogSourceError(f"server did not report a size for {self.path}")
        return int(size)

    def _read_from(self, offset: int, length: int) -> bytes:
        chunks: list[bytes] = []
        self._ftp.retrbinary(f"RETR {self.path}", chunks.append, rest=offset)
        return b"".join(chunks)

    def _disconnect(self) -> None:
        if self._ftp is not None:
            try:
                self._ftp.quit()  # polite QUIT; falls back to a hard close below
            except Exception:
                self._ftp.close()
            self._ftp = None


class SftpLogSource(RemoteLogSource):
    """Tail over SFTP (``sftp://``) — the legacy ``sftpytail``. Needs ``paramiko``.

    Authenticates with the password in the URL if there is one, otherwise with the agent and the
    usual ``~/.ssh`` keys. Unknown host keys are accepted with a warning (the same practical
    stance as the legacy plugin); pass ``strict_host_key=True`` to refuse them instead.
    """

    _client: Any = None
    _sftp: Any = None

    def __init__(self, url: str, *, strict_host_key: bool = False, **kwargs: Any) -> None:
        super().__init__(url, **kwargs)
        self._strict = strict_host_key

    def _connect(self) -> None:
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise LogSourceError(
                "sftp:// game logs need paramiko; install it with: pip install b3ng[sftp]"
            ) from exc

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(
            paramiko.RejectPolicy() if self._strict else paramiko.WarningPolicy()
        )
        client.connect(
            self.parts.hostname or "",
            port=self.parts.port or 22,
            username=self.username,
            password=self.password,
            timeout=self._timeout,
            look_for_keys=self.password is None,
            allow_agent=self.password is None,
        )
        self._client = client
        self._sftp = client.open_sftp()

    def _remote_size(self) -> int:
        return int(self._sftp.stat(self.path).st_size)

    def _read_from(self, offset: int, length: int) -> bytes:
        with self._sftp.open(self.path, "rb") as fh:
            fh.seek(offset)
            return bytes(fh.read(length))

    def _disconnect(self) -> None:
        for handle in (self._sftp, self._client):
            if handle is not None:
                try:
                    handle.close()
                except Exception:  # pragma: no cover - already broken
                    pass
        self._sftp = None
        self._client = None


class HttpLogSource(RemoteLogSource):
    """Tail a log published over HTTP(S) — the legacy ``httpytail``.

    Stateless: each poll is a ``HEAD`` for the size and a ranged ``GET`` for the new bytes. Needs
    a server that honours ``Range`` requests; if one ignores the header and returns the whole file
    we slice locally rather than replaying it, so it still works, just wastefully.
    """

    @property
    def _request_url(self) -> str:
        """The URL with the userinfo stripped — credentials go in the Authorization header."""
        host = self.parts.hostname or ""
        netloc = f"{host}:{self.parts.port}" if self.parts.port else host
        return self.parts._replace(netloc=netloc).geturl()

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "b3ng"}
        if self.username is not None:
            token = b64encode(f"{self.username}:{self.password or ''}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        return headers

    def _connect(self) -> None:
        self._remote_size()  # nothing to hold open; prove the URL is reachable instead

    def _remote_size(self) -> int:
        from urllib.request import Request, urlopen

        req = Request(self._request_url, headers=self._headers(), method="HEAD")
        with urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 - scheme is validated
            length = resp.headers.get("Content-Length")
        if length is None:
            raise LogSourceError(
                f"{self.safe_url} reports no Content-Length; it cannot be tailed by byte offset"
            )
        return int(length)

    def _read_from(self, offset: int, length: int) -> bytes:
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        headers = self._headers() | {"Range": f"bytes={offset}-{offset + length - 1}"}
        req = Request(self._request_url, headers=headers)
        try:
            with urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 - scheme is validated
                data = resp.read()
                status = resp.status
        except HTTPError as exc:
            if exc.code == 416:  # Range Not Satisfiable: the file shrank under us
                return b""
            raise
        if status != 206:
            # Range ignored: we got the whole file, so take our slice of it.
            data = data[offset : offset + length]
        return bytes(data)

    def _disconnect(self) -> None:
        return None


def create_log_source(
    spec: str,
    *,
    encoding: str = "latin-1",
    from_start: bool = False,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    timeout: float = 10.0,
    max_gap: int = DEFAULT_MAX_GAP,
    clock: Clock | None = None,
) -> LogSource:
    """Build the log source ``spec`` asks for.

    ``spec`` is either a filesystem path or a URL: ``ftp://``, ``ftps://``, ``sftp://``,
    ``http://``, ``https://`` or an explicit ``file://``. Anything whose scheme we do not
    recognise is a path — that keeps ``F:\\logs\\games_mp.log`` (scheme ``f``) a path on Windows.
    """
    scheme = urlsplit(spec).scheme.lower()
    if scheme not in URL_SCHEMES:
        return FileLogSource(spec, encoding=encoding, from_start=from_start)

    if scheme == "file":
        from urllib.request import url2pathname

        return FileLogSource(
            url2pathname(urlsplit(spec).path), encoding=encoding, from_start=from_start
        )

    kinds: dict[str, type[RemoteLogSource]] = {
        "ftp": FtpLogSource,
        "ftps": FtpLogSource,
        "sftp": SftpLogSource,
        "http": HttpLogSource,
        "https": HttpLogSource,
    }
    return kinds[scheme](
        spec,
        encoding=encoding,
        from_start=from_start,
        poll_interval=poll_interval,
        timeout=timeout,
        max_gap=max_gap,
        clock=clock,
    )
