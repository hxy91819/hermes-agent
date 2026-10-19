"""A desktop in another filesystem must not move host child state into it."""
import subprocess
import sys

import pytest

from tools.bot_desktop import runtime, sandbox_host


@pytest.mark.parametrize("sandbox", [True, False])
def test_host_child_uses_config_home_of_its_own_desktop(tmp_path, monkeypatch, sandbox):
    host_config = tmp_path / "host-config"
    desktop_config = tmp_path / "desktop-config"
    base = {"XDG_CONFIG_HOME": str(host_config), "HOME": str(tmp_path)}
    published = {}
    monkeypatch.setattr(runtime, "published_env", lambda: dict(published))
    monkeypatch.setattr(runtime, "touch_activity", lambda: None)
    monkeypatch.setattr(sandbox_host, "_read_marker", lambda: {"backend": "SSHEnvironment"} if sandbox else {})
    monkeypatch.setattr("tools.bot_desktop.browser.env_for_agent", lambda env: env)

    # First browser call builds its env before starting the desktop; subsequent
    # calls observe the launched desktop's XDG state. Both spawn on the host.
    for call in range(3):
        child_env = runtime.desktop_env(base)
        result = subprocess.run(
            [sys.executable, "-c", "import os,pathlib; p=pathlib.Path(os.environ['XDG_CONFIG_HOME'])/'browser-harness'/'tmp'; p.mkdir(parents=True,exist_ok=True); print(p)"],
            env=child_env, text=True, capture_output=True, check=True,
        )
        expected = host_config if sandbox or call == 0 else desktop_config
        assert result.stdout.strip() == str(expected / "browser-harness" / "tmp")
        published.update(XDG_CONFIG_HOME=str(desktop_config), DISPLAY=":20")
    assert published["XDG_CONFIG_HOME"] == str(desktop_config)
    assert desktop_config.exists() is (not sandbox)
