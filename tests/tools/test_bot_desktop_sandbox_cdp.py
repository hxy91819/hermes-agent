"""Reuse a live browser in the sandbox without reconfiguring its owning daemon."""
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.bot_desktop import sandbox_host
from tools import browser_tool_session as session

XAUTHORITY = "/desktop/alpha/Xauthority"


class _FakeChromium:
    """A live listener whose parent carries the desktop identity.

    The process itself gets an empty environment like Chromium; the parent is the daemon that has the
    desktop's ``XAUTHORITY``. It is launched with the profile's ``--user-data-dir`` so it looks like the
    browser whose ``SingletonLock``/``DevToolsActivePort`` files live in ``profile``.
    """

    _CHILD = r"""
import os, socket, sys
root = sys.argv[1]
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", 0))
s.listen()
print(os.getpid(), s.getsockname()[1], flush=True)
for line in sys.stdin:
    if line.strip() == "close":
        s.close()
        print("CLOSED", flush=True)
        break
import time
time.sleep(120)
"""

    def __init__(self, profile: Path, *, profile_flag: bool = True,
                 xauthority: str = XAUTHORITY, session: str = "owner"):
        self.profile = profile
        self.profile_flag = profile_flag
        self.xauthority = xauthority
        self.session = session
        self.process = None
        self.pid = None
        self.port = None

    def __enter__(self):
        parent = "import subprocess,sys; subprocess.run([sys.executable,'-c',*sys.argv[1:]],check=True,env={})"
        argv = [sys.executable, "-c", parent, self._CHILD, str(self.profile)]
        if self.profile_flag:
            argv.append(f"--user-data-dir={self.profile}")
        self.process = subprocess.Popen(
            argv,
            env={**os.environ, "XAUTHORITY": self.xauthority, "AGENT_BROWSER_SESSION": self.session},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True)
        line = self.process.stdout.readline().strip()
        pid_text, port_text = line.split()
        self.pid, self.port = int(pid_text), int(port_text)
        _write_profile(self.profile, self.pid, self.port)
        return self

    def close_listener(self):
        self.process.stdin.write("close\n")
        self.process.stdin.flush()
        assert self.process.stdout.readline().strip() == "CLOSED"

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=5)

    def __exit__(self, *exc_info):
        self.stop()


def _write_profile(root: Path, pid: int, port: int) -> None:
    lock = root / "SingletonLock"
    lock.unlink(missing_ok=True)
    lock.symlink_to(f"sandbox-{pid}")
    (root / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/test\n", encoding="utf-8")


def _browser_cdp_port(root: Path, monkeypatch) -> int | None:
    remote = SimpleNamespace()
    monkeypatch.setattr(sandbox_host, "browser_profile_dir", lambda env: str(root))
    monkeypatch.setattr(sandbox_host, "_user_for", lambda env: None)
    monkeypatch.setattr(sandbox_host.streams, "run_in", lambda env, argv, **_: subprocess.run(
        [sys.executable, *argv[1:]], capture_output=True, check=True))
    return sandbox_host.browser_cdp_port(remote, exclude_session="other", xauthority=XAUTHORITY)


@pytest.mark.platforms("linux")
def test_sandbox_browser_attach_uses_remote_profile_and_excludes_owner(tmp_path, monkeypatch):
    with _FakeChromium(tmp_path) as browser:
        remote = SimpleNamespace()
        monkeypatch.setattr(session, "_shares_bot_desktop_browser", lambda _: True)
        monkeypatch.setattr(session, "_browser_in_sandbox", lambda: True)
        monkeypatch.setattr("tools.bot_desktop.runtime._sandbox_env", lambda **_: remote)
        monkeypatch.setattr(sandbox_host, "browser_profile_dir", lambda env: str(tmp_path))
        monkeypatch.setattr(sandbox_host, "_user_for", lambda env: None)
        monkeypatch.setattr(sandbox_host.streams, "run_in", lambda env, argv, **_: subprocess.run(
            [sys.executable, *argv[1:]], capture_output=True, check=True))
        desktop = {"XAUTHORITY": XAUTHORITY}
        monkeypatch.setattr("tools.bot_desktop.runtime.published_env", lambda: dict(desktop))
        assert session._bot_desktop_attach_port({"session_name": "other"}) == browser.port
        assert session._bot_desktop_attach_port({"session_name": "owner"}) is None
        desktop["XAUTHORITY"] = "/desktop/beta/Xauthority"
        assert session._bot_desktop_attach_port({"session_name": "other"}) is None
        desktop.clear()
        assert session._bot_desktop_attach_port({"session_name": "other"}) is None


@pytest.mark.platforms("linux")
def test_sandbox_cdp_rejects_lock_and_port_from_different_processes(tmp_path, monkeypatch):
    with _FakeChromium(tmp_path) as owner, _FakeChromium(tmp_path) as other:
        _write_profile(tmp_path, owner.pid, other.port)
        assert _browser_cdp_port(tmp_path, monkeypatch) is None


@pytest.mark.platforms("linux")
def test_sandbox_cdp_rejects_port_reused_after_owner_stopped_listening(tmp_path, monkeypatch):
    with _FakeChromium(tmp_path) as owner:
        owner.close_listener()
        reused = socket.socket()
        reused.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        reused.bind(("127.0.0.1", owner.port))
        reused.listen()
        try:
            assert _browser_cdp_port(tmp_path, monkeypatch) is None
        finally:
            reused.close()


@pytest.mark.platforms("linux")
def test_sandbox_cdp_rejects_stale_lock_with_a_live_port(tmp_path, monkeypatch):
    with _FakeChromium(tmp_path) as stale, _FakeChromium(tmp_path) as live:
        stale.stop()
        _write_profile(tmp_path, stale.pid, live.port)
        assert _browser_cdp_port(tmp_path, monkeypatch) is None


@pytest.mark.platforms("linux")
def test_sandbox_cdp_rejects_stale_port_file_without_a_listener(tmp_path, monkeypatch):
    with _FakeChromium(tmp_path) as owner:
        owner.close_listener()
        assert _browser_cdp_port(tmp_path, monkeypatch) is None


@pytest.mark.platforms("linux")
def test_sandbox_cdp_rejects_lock_reused_by_a_process_outside_the_profile(tmp_path, monkeypatch):
    with _FakeChromium(tmp_path, profile_flag=False) as unrelated:
        _write_profile(tmp_path, unrelated.pid, unrelated.port)
        assert _browser_cdp_port(tmp_path, monkeypatch) is None


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("profile_name", ["profile", "profile with spaces"])
@pytest.mark.parametrize("suffix,accepted", [("", True), ("-other", False), (" extra", False)])
def test_sandbox_cdp_handles_chromium_flattened_process_title(tmp_path, monkeypatch, suffix, accepted, profile_name):
    tmp_path = tmp_path / profile_name
    tmp_path.mkdir()
    # Actual packaged Chromium rewrites /proc/<pid>/cmdline into one space-joined argv[0].
    # Keep the real parent identity, listening socket and owner fds; vary only those bytes.
    with _FakeChromium(tmp_path) as browser:
        remote = SimpleNamespace()
        monkeypatch.setattr(sandbox_host, "browser_profile_dir", lambda env: str(tmp_path))
        monkeypatch.setattr(sandbox_host, "_user_for", lambda env: None)
        raw = (f"/opt/chromium/chrome --remote-debugging-port=0 --user-data-dir={tmp_path}{suffix} "
               "--no-sandbox --disable-dev-shm-usage").encode() + b"\0"
        prefix = ("import pathlib\n_original_read_bytes = pathlib.Path.read_bytes\n"
                  "def _proc_bytes(path):\n"
                  f"    if str(path) == '/proc/{browser.pid}/cmdline': return {raw!r}\n"
                  "    return _original_read_bytes(path)\n"
                  "pathlib.Path.read_bytes = _proc_bytes\n")
        def run_in(env, argv, **kwargs):
            return subprocess.run([sys.executable, "-c", prefix + argv[2], *argv[3:]],
                                  capture_output=True, check=True)
        monkeypatch.setattr(sandbox_host.streams, "run_in", run_in)
        value = sandbox_host.browser_cdp_port(remote, exclude_session="other", xauthority=XAUTHORITY)
        assert value == (browser.port if accepted else None)
