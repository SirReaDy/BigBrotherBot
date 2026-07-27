"""Remote log tailing (PARITY P4): the shared offset/rotation/back-off engine plus the
FTP, SFTP and HTTP transports and the URL → source factory.

The transports are exercised against stand-ins for ftplib / paramiko / urllib rather than real
servers, so what is asserted is the wire behaviour we depend on: resume with ``REST``/``seek``/
``Range``, binary mode, passive FTP, basic auth, and the 200/206/416 cases.
"""

from __future__ import annotations

import sys
import types

import pytest

from b3.core.clock import FakeClock
from b3.net.logsource import (
    FileLogSource,
    FtpLogSource,
    HttpLogSource,
    LogSourceError,
    RemoteLogSource,
    SftpLogSource,
    create_log_source,
)


class FakeRemote(RemoteLogSource):
    """A remote log that lives in a bytes attribute, with a switch to make polls fail."""

    def __init__(self, url: str = "ftp://user:secret@host/games_mp.log", **kwargs) -> None:
        super().__init__(url, **kwargs)
        self.content = b""
        self.fail = False
        self.connects = 0
        self.disconnects = 0
        self.reads: list[tuple[int, int]] = []

    def _connect(self) -> None:
        if self.fail:
            raise OSError("connection refused")
        self.connects += 1

    def _remote_size(self) -> int:
        if self.fail:
            raise OSError("connection reset")
        return len(self.content)

    def _read_from(self, offset: int, length: int) -> bytes:
        self.reads.append((offset, length))
        return self.content[offset : offset + length]

    def _disconnect(self) -> None:
        self.disconnects += 1


# -- the shared tailing engine ---------------------------------------------------------------


def test_tails_from_the_end_by_default():
    src = FakeRemote()
    src.content = b"history1\nhistory2\n"
    src.open()

    assert src.read_lines() == []  # existing history is not replayed

    src.content += b"new\n"
    assert src.read_lines() == ["new"]


def test_from_start_replays_the_whole_log():
    src = FakeRemote(from_start=True)
    src.content = b"line1\nline2\n"
    src.open()
    assert src.read_lines() == ["line1", "line2"]
    assert src.read_lines() == []


def test_reads_resume_at_the_stored_offset():
    src = FakeRemote(from_start=True)
    src.content = b"a\n"
    src.open()
    assert src.read_lines() == ["a"]

    src.content += b"bb\ncc\n"
    assert src.read_lines() == ["bb", "cc"]
    assert src.reads == [(0, 2), (2, 6)]  # only the new bytes were ever fetched


def test_partial_line_is_buffered_between_polls():
    src = FakeRemote(from_start=True)
    src.open()

    src.content = b"par"
    assert src.read_lines() == []

    src.content += b"tial\n"
    assert src.read_lines() == ["partial"]


def test_rotation_restarts_from_the_beginning():
    src = FakeRemote(from_start=True)
    src.content = b"old1\nold2\n"
    src.open()
    assert src.read_lines() == ["old1", "old2"]

    src.content = b"fresh\n"  # server rolled the log: it is now shorter than our offset
    assert src.read_lines() == ["fresh"]


def test_large_gap_is_skipped_and_the_partial_first_line_dropped():
    src = FakeRemote(from_start=True, max_gap=20)
    src.content = b"x" * 100 + b"\nkept1\nkept2\n"
    src.open()

    # Only the last 20 bytes are fetched, and the fragment before the first newline is discarded.
    assert src.read_lines() == ["kept1", "kept2"]
    assert src.reads == [(len(src.content) - 20, 20)]


def test_max_gap_zero_reads_everything():
    src = FakeRemote(from_start=True, max_gap=0)
    src.content = b"line\n" * 10_000
    src.open()
    assert len(src.read_lines()) == 10_000


def test_read_failure_backs_off_then_reconnects_and_resumes():
    clock = FakeClock()
    src = FakeRemote(from_start=True, clock=clock, retry_interval=5.0)
    src.content = b"before\n"
    src.open()
    assert src.read_lines() == ["before"]

    src.fail = True
    src.content += b"during\n"
    assert src.read_lines() == []  # the error is swallowed: the bot must keep running
    assert src.read_lines() == []  # still inside the back-off window: no reconnect attempt
    assert src.connects == 1

    clock.advance(5.0)
    src.fail = False
    # Reconnects and picks up exactly where it left off — nothing replayed, nothing lost.
    assert src.read_lines() == ["during"]
    assert src.connects == 2


def test_backoff_grows_exponentially_and_is_capped():
    clock = FakeClock()
    src = FakeRemote(clock=clock, retry_interval=5.0, retry_max=20.0)
    src.open()
    src.fail = True

    for expected in (5.0, 10.0, 20.0, 20.0):
        start = clock.now()
        assert src.read_lines() == []
        assert src._retry_at == pytest.approx(start + expected)
        clock.advance(expected)


def test_open_raises_so_a_bad_url_fails_at_startup():
    src = FakeRemote()
    src.fail = True
    with pytest.raises(LogSourceError, match="cannot open"):
        src.open()


def test_url_without_a_host_is_rejected():
    with pytest.raises(LogSourceError, match="no host"):
        FakeRemote("ftp:///games_mp.log")


def test_password_is_redacted_in_the_loggable_url():
    src = FakeRemote("ftp://bob:hunter2@example.com:2121/logs/games_mp.log")
    assert "hunter2" not in src.safe_url
    assert src.safe_url == "ftp://bob:***@example.com:2121/logs/games_mp.log"
    assert src.password == "hunter2"


def test_percent_encoded_credentials_and_path_are_decoded():
    src = FakeRemote("ftp://my%40user:p%40ss@host/my%20logs/games_mp.log")
    assert src.username == "my@user"
    assert src.password == "p@ss"
    assert src.path == "/my logs/games_mp.log"


def test_close_disconnects_once():
    src = FakeRemote()
    src.open()
    src.close()
    src.close()
    assert src.disconnects == 1


# -- the factory -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("games_mp.log", FileLogSource),
        ("/var/log/games_mp.log", FileLogSource),
        (r"F:\logs\games_mp.log", FileLogSource),  # 'F:' must not look like a URL scheme
        ("ftp://host/games_mp.log", FtpLogSource),
        ("ftps://host/games_mp.log", FtpLogSource),
        ("sftp://host/games_mp.log", SftpLogSource),
        ("http://host/games_mp.log", HttpLogSource),
        ("HTTPS://host/games_mp.log", HttpLogSource),
    ],
)
def test_factory_dispatches_on_scheme(spec, expected):
    assert isinstance(create_log_source(spec), expected)


def test_factory_handles_file_urls(tmp_path):
    log = tmp_path / "games_mp.log"
    log.write_bytes(b"hello\n")
    src = create_log_source(log.as_uri(), from_start=True)
    src.open()
    assert src.read_lines() == ["hello"]
    src.close()


def test_factory_passes_the_tuning_knobs_through():
    src = create_log_source("ftp://host/g.log", poll_interval=7.5, timeout=3.0, max_gap=99)
    assert src.poll_interval == 7.5
    assert src._timeout == 3.0
    assert src._max_gap == 99


def test_local_source_is_not_blocking_but_remote_is():
    assert create_log_source("games_mp.log").blocking is False
    assert create_log_source("ftp://host/g.log").blocking is True


# -- FTP -------------------------------------------------------------------------------------


class FakeFtp:
    instances: list[FakeFtp] = []
    # One remote file behind every connection, so a reconnect sees what the last session saw.
    store = {"content": b""}

    def __init__(self, timeout=None):
        self.timeout = timeout
        self.commands: list[str] = []
        self.connected_to = None
        self.login_as = None
        self.passive = None
        self.quit_called = False
        FakeFtp.instances.append(self)

    @property
    def content(self) -> bytes:
        return FakeFtp.store["content"]

    @content.setter
    def content(self, value: bytes) -> None:
        FakeFtp.store["content"] = value

    def connect(self, host, port):
        self.connected_to = (host, port)

    def login(self, user, passwd):
        self.login_as = (user, passwd)

    def set_pasv(self, on):
        self.passive = on

    def voidcmd(self, cmd):
        self.commands.append(cmd)

    def size(self, path):
        self.last_size_path = path
        return len(self.content)

    def retrbinary(self, cmd, callback, rest=0):
        self.commands.append(f"{cmd} rest={rest}")
        callback(self.content[rest:])

    def quit(self):
        self.quit_called = True


@pytest.fixture
def fake_ftp(monkeypatch):
    import ftplib

    FakeFtp.instances = []
    FakeFtp.store = {"content": b""}
    monkeypatch.setattr(ftplib, "FTP", FakeFtp)
    return FakeFtp


def test_ftp_connects_binary_and_passive(fake_ftp):
    src = FtpLogSource("ftp://bob:pw@host:2121/logs/games_mp.log", from_start=True)
    src.open()
    ftp = fake_ftp.instances[0]

    assert ftp.connected_to == ("host", 2121)
    assert ftp.login_as == ("bob", "pw")
    assert ftp.passive is True
    assert "TYPE I" in ftp.commands  # SIZE/REST are only meaningful in binary mode


def test_ftp_defaults_to_port_21_and_anonymous(fake_ftp):
    src = FtpLogSource("ftp://host/games_mp.log")
    src.open()
    ftp = fake_ftp.instances[0]
    assert ftp.connected_to == ("host", 21)
    assert ftp.login_as == ("anonymous", "")


def test_ftp_resumes_with_rest(fake_ftp):
    src = FtpLogSource("ftp://host/games_mp.log", from_start=True)
    src.open()
    ftp = fake_ftp.instances[0]

    ftp.content = b"one\n"
    assert src.read_lines() == ["one"]
    ftp.content += b"two\n"
    assert src.read_lines() == ["two"]

    assert "RETR /games_mp.log rest=4" in ftp.commands  # second fetch started after "one\n"


def test_ftp_quits_on_close(fake_ftp):
    src = FtpLogSource("ftp://host/games_mp.log")
    src.open()
    src.close()
    assert fake_ftp.instances[0].quit_called is True


def test_ftp_reconnects_after_a_dropped_control_connection(fake_ftp):
    clock = FakeClock()
    src = FtpLogSource("ftp://host/games_mp.log", from_start=True, clock=clock)
    src.open()
    fake_ftp.instances[0].content = b"first\n"
    assert src.read_lines() == ["first"]

    def boom(path):
        raise OSError("421 idle timeout")

    fake_ftp.instances[0].size = boom
    assert src.read_lines() == []

    clock.advance(60)
    fake_ftp.store["content"] = b"first\nsecond\n"  # the log kept growing while we were away
    assert src.read_lines() == ["second"]  # resumed at offset 6, not from the top
    assert len(fake_ftp.instances) == 2  # a fresh control connection was opened


# -- SFTP ------------------------------------------------------------------------------------


class FakeSftpFile:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self._pos = 0

    def seek(self, pos):
        self._pos = pos

    def read(self, length):
        return self._content[self._pos : self._pos + length]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSftpClient:
    def __init__(self, owner):
        self.owner = owner

    def stat(self, path):
        return types.SimpleNamespace(st_size=len(self.owner.content))

    def open(self, path, mode):
        return FakeSftpFile(self.owner.content)

    def close(self):
        self.owner.closed = True


class FakeSshClient:
    instances: list[FakeSshClient] = []

    def __init__(self):
        self.content = b""
        self.connect_kwargs = None
        self.policy = None
        self.system_keys_loaded = False
        self.closed = False
        FakeSshClient.instances.append(self)

    def load_system_host_keys(self):
        self.system_keys_loaded = True

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, host, **kwargs):
        self.connect_kwargs = {"host": host, **kwargs}

    def open_sftp(self):
        return FakeSftpClient(self)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_paramiko(monkeypatch):
    FakeSshClient.instances = []
    module = types.ModuleType("paramiko")
    module.SSHClient = FakeSshClient
    module.WarningPolicy = type("WarningPolicy", (), {})
    module.RejectPolicy = type("RejectPolicy", (), {})
    monkeypatch.setitem(sys.modules, "paramiko", module)
    return module


def test_sftp_reads_by_seeking_to_the_offset(fake_paramiko):
    src = SftpLogSource("sftp://bob:pw@host/var/games_mp.log", from_start=True)
    src.open()
    ssh = FakeSshClient.instances[0]

    ssh.content = b"alpha\n"
    assert src.read_lines() == ["alpha"]
    ssh.content += b"beta\n"
    assert src.read_lines() == ["beta"]


def test_sftp_password_auth_skips_keys_and_agent(fake_paramiko):
    SftpLogSource("sftp://bob:pw@host/g.log").open()
    kwargs = FakeSshClient.instances[0].connect_kwargs
    assert kwargs["host"] == "host"
    assert kwargs["port"] == 22
    assert kwargs["username"] == "bob"
    assert kwargs["password"] == "pw"
    assert kwargs["look_for_keys"] is False
    assert kwargs["allow_agent"] is False


def test_sftp_without_a_password_uses_keys_and_agent(fake_paramiko):
    SftpLogSource("sftp://bob@host:2222/g.log").open()
    kwargs = FakeSshClient.instances[0].connect_kwargs
    assert kwargs["port"] == 2222
    assert kwargs["password"] is None
    assert kwargs["look_for_keys"] is True
    assert kwargs["allow_agent"] is True


def test_sftp_host_key_policy_is_warn_by_default_and_reject_when_strict(fake_paramiko):
    SftpLogSource("sftp://host/g.log").open()
    assert isinstance(FakeSshClient.instances[0].policy, fake_paramiko.WarningPolicy)
    assert FakeSshClient.instances[0].system_keys_loaded is True

    SftpLogSource("sftp://host/g.log", strict_host_key=True).open()
    assert isinstance(FakeSshClient.instances[1].policy, fake_paramiko.RejectPolicy)


def test_sftp_without_paramiko_explains_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "paramiko", None)  # import paramiko -> ImportError
    with pytest.raises(LogSourceError, match="paramiko"):
        SftpLogSource("sftp://host/g.log").open()


# -- HTTP ------------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None):
        self._body = body
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_http(monkeypatch):
    """Serve ``state['content']`` over a fake urlopen that honours Range like a real server."""
    import urllib.request

    state = {"content": b"", "requests": [], "range_support": True}

    def fake_urlopen(req, timeout=None):
        state["requests"].append(req)
        content = state["content"]
        if req.get_method() == "HEAD":
            return FakeResponse(headers={"Content-Length": str(len(content))})
        rng = req.headers.get("Range")
        if rng is None or not state["range_support"]:
            return FakeResponse(content, status=200)
        start, end = rng.removeprefix("bytes=").split("-")
        start, end = int(start), int(end)
        if start >= len(content):
            from urllib.error import HTTPError

            raise HTTPError(req.full_url, 416, "Range Not Satisfiable", {}, None)
        return FakeResponse(content[start : end + 1], status=206)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return state


def test_http_tails_with_range_requests(fake_http):
    src = HttpLogSource("http://host/games_mp.log", from_start=True)
    src.open()

    fake_http["content"] = b"one\n"
    assert src.read_lines() == ["one"]
    fake_http["content"] += b"two\n"
    assert src.read_lines() == ["two"]

    ranges = [r.headers.get("Range") for r in fake_http["requests"] if r.headers.get("Range")]
    assert ranges == ["bytes=0-3", "bytes=4-7"]


def test_http_slices_locally_when_the_server_ignores_range(fake_http):
    fake_http["range_support"] = False
    src = HttpLogSource("http://host/games_mp.log", from_start=True)
    src.open()

    fake_http["content"] = b"one\ntwo\n"
    assert src.read_lines() == ["one", "two"]
    fake_http["content"] += b"three\n"
    assert src.read_lines() == ["three"]  # not the whole file again


def test_http_credentials_go_in_a_basic_auth_header_not_the_url(fake_http):
    src = HttpLogSource("http://bob:pw@host/games_mp.log")
    src.open()
    req = fake_http["requests"][0]

    assert req.full_url == "http://host/games_mp.log"
    assert req.headers["Authorization"] == "Basic Ym9iOnB3"  # bob:pw
    assert "pw" not in src.safe_url


def test_http_without_content_length_is_a_clear_error(monkeypatch):
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: FakeResponse(headers={})
    )
    with pytest.raises(LogSourceError, match="Content-Length"):
        HttpLogSource("http://host/games_mp.log").open()


def test_http_416_is_treated_as_no_new_data(fake_http):
    src = HttpLogSource("http://host/games_mp.log", from_start=True)
    src.open()
    fake_http["content"] = b"line\n"
    assert src.read_lines() == ["line"]

    # The server shrank the file between our HEAD and our GET: no crash, no bogus lines.
    src._offset = 0
    fake_http["content"] = b""
    assert src.read_lines() == []
