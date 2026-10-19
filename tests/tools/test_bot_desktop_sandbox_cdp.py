"""Reuse a live browser in the sandbox without reconfiguring its owning daemon."""
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tools.bot_desktop import sandbox_host
from tools import browser_tool_session as session


@pytest.mark.platforms("linux")
def test_sandbox_browser_attach_uses_remote_profile_and_excludes_owner(tmp_path, monkeypatch):
    child = """import os,pathlib,socket,sys
root=pathlib.Path(sys.argv[1]);s=socket.socket();s.bind(('127.0.0.1',0));s.listen()
(root/'SingletonLock').symlink_to('sandbox-'+str(os.getpid()))
(root/'DevToolsActivePort').write_text(str(s.getsockname()[1])+'\\n/devtools/browser/test')
print('READY',flush=True)
while True:
 c,_=s.accept();c.close()
"""
    parent = "import subprocess,sys; subprocess.run([sys.executable,'-c',sys.argv[1],sys.argv[2]],check=True)"
    process = subprocess.Popen([sys.executable, "-c", parent, child, str(tmp_path)],
                               env={**os.environ, "AGENT_BROWSER_SESSION": "owner"},
                               stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert process.stdout.readline().strip() == "READY"
        port = int((tmp_path / "DevToolsActivePort").read_text().splitlines()[0])
        remote = SimpleNamespace()
        monkeypatch.setattr(session, "_shares_bot_desktop_browser", lambda _: True)
        monkeypatch.setattr(session, "_browser_in_sandbox", lambda: True)
        monkeypatch.setattr("tools.bot_desktop.runtime._sandbox_env", lambda **_: remote)
        monkeypatch.setattr(sandbox_host, "browser_profile_dir", lambda env: str(tmp_path))
        monkeypatch.setattr(sandbox_host, "_user_for", lambda env: None)
        monkeypatch.setattr(sandbox_host.streams, "run_in", lambda env, argv, **_: subprocess.run(
            [sys.executable, *argv[1:]], capture_output=True, check=True))
        assert session._bot_desktop_attach_port({"session_name": "other"}) == port
        assert session._bot_desktop_attach_port({"session_name": "owner"}) is None
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    assert session._bot_desktop_attach_port({"session_name": "other"}) is None
