"""Final-response MEDIA on a split (gateway-on-host, files-in-ssh-sandbox) instance.

Reproduces the 2026-11-11 QQ report: the agent wrote ``MEDIA:/root/deliver/x.jpg``; by the time the
adapter extracts the final response the turn has cleared its session ContextVars, so
``fetch_remote_media`` looked up ``"default"`` while the SSHEnvironment is registered under
``session:<key>``; it found nothing and the tag vanished without a user-visible notice. (If the env
was idle-reaped or evicted meanwhile, it must be re-created under that same key.)
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import media_fetch
from gateway.platforms import base as base_mod
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session_context import clear_session_vars, set_session_vars


class _FakeSSHEnv:
    _remote_home = "/root"

    def __init__(self, files):
        self.files = files
        self.fetched = []

    def fetch_realpath(self, remote_path):
        return remote_path

    def fetch_file(self, remote_path, local_dest, *, max_bytes):
        self.fetched.append(remote_path)
        Path(local_dest).write_bytes(self.files[remote_path])

    def cleanup(self):
        pass


@pytest.fixture
def ssh_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.delenv("HERMES_MEDIA_DELIVERY_STRICT", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(base_mod, "_tenv", lambda name, default="": "ssh" if name == "TERMINAL_ENV" else default)
    monkeypatch.setattr("gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "cache" / "documents")
    monkeypatch.setattr("gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS", (tmp_path / "cache" / "documents",))
    from tools import terminal_tool
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    return terminal_tool


def _after_turn_context():
    """Simulate the adapter delivery side: the turn bound and then cleared its session vars."""
    tokens = set_session_vars(platform="qqbot", session_key="agent:main:qqbot:dm:U1", session_id="sid-1")
    clear_session_vars(tokens)


def test_env_still_registered_under_session_slot_is_found_after_vars_cleared(ssh_backend, monkeypatch):
    env = _FakeSSHEnv({"/root/deliver/f1.jpg": b"jpg"})
    ssh_backend._active_environments["session:agent:main:qqbot:dm:U1"] = env
    monkeypatch.setattr("tools.terminal_tool_lifecycle.ensure_task_env",
                        lambda task_id=None: pytest.fail("must reuse the live env, not re-create"))
    _after_turn_context()
    delivered = BasePlatformAdapter.filter_media_delivery_paths(
        [("/root/deliver/f1.jpg", False)], session_key="agent:main:qqbot:dm:U1")
    assert [Path(p).read_bytes() for p, _ in delivered] == [b"jpg"]


def test_reaped_env_is_reattached_under_the_session_key(ssh_backend, monkeypatch):
    env = _FakeSSHEnv({"/root/deliver/mijia_all.zip": b"PK"})
    seen = {}

    def fake_ensure(task_id=None):
        from gateway.session_context import get_session_env
        from tools.terminal_tool import _resolve_container_task_id
        seen["slot"] = _resolve_container_task_id(task_id)
        seen["bound_key"] = get_session_env("HERMES_SESSION_KEY")
        return env

    monkeypatch.setattr("tools.terminal_tool_lifecycle.ensure_task_env", fake_ensure)
    _after_turn_context()
    delivered = BasePlatformAdapter.filter_media_delivery_paths(
        [("/root/deliver/mijia_all.zip", False)], session_key="agent:main:qqbot:dm:U1")
    assert [Path(p).read_bytes() for p, _ in delivered] == [b"PK"]
    # Same slot the terminal tool used during the turn, not the cleared "default".
    assert seen == {"slot": "session:agent:main:qqbot:dm:U1", "bound_key": "agent:main:qqbot:dm:U1"}
    from gateway.session_context import get_session_env
    assert get_session_env("HERMES_SESSION_KEY") == ""  # binding is scoped to the fetch


def test_reattach_failure_is_reported_as_dropped(ssh_backend, monkeypatch, caplog):
    monkeypatch.setattr("tools.terminal_tool_lifecycle.ensure_task_env", lambda task_id=None: None)
    _after_turn_context()
    dropped = []
    caplog.set_level("WARNING")
    out = BasePlatformAdapter.filter_media_delivery_paths(
        [("/root/deliver/f1.jpg", False)], session_key="agent:main:qqbot:dm:U1", dropped=dropped)
    assert out == []
    assert dropped == [{"path": "/root/deliver/f1.jpg", "reason": "not found on this host"}]
    assert "sandbox unavailable" in caplog.text


class _RecordingAdapter:
    """Minimal stand-in exercising the real _deliver_attachments drop-notice branch."""
    name = "qqbot"

    def __init__(self):
        self.notices = []

    def _get_human_delay(self):
        return 0.0

    async def _send_image_batch(self, *a, **k):
        pass

    async def _deliver_media_attachments(self, *a, **k):
        pass

    async def _notify_media_delivery_failure(self, chat_id, path, *, is_voice=False, metadata=None):
        self.notices.append((chat_id, path))


def test_dropped_media_produces_user_notice_and_failed_delivery_record():
    adapter = _RecordingAdapter()
    extracted = base_mod._ExtractedResponse(
        text_content="here you go", images=[], media_files=[], local_files=[],
        force_document_attachments=False, pre_extract="here you go\nMEDIA:/root/deliver/f1.jpg",
        dropped_media=[{"path": "/root/deliver/f1.jpg", "reason": "not found on this host"}])
    event = SimpleNamespace(source=SimpleNamespace(chat_id="U1"))
    records = []
    asyncio.run(BasePlatformAdapter._deliver_attachments(
        adapter, event, extracted, {}, anything_sent=True, record_delivery=records.append))
    assert adapter.notices == [("U1", "/root/deliver/f1.jpg")]
    assert len(records) == 1 and isinstance(records[0], SendResult) and not records[0].success


def test_per_turn_cleanup_does_not_unregister_the_session_slot(ssh_backend):
    """Pins the lifecycle assumption: gateway turns call cleanup_vm(<raw session_id>), but the env is
    keyed ``session:<key>``, so it survives to delivery — the miss is purely the cleared lookup key."""
    from gateway.session_context import set_session_vars
    from tools.terminal_tool_lifecycle import cleanup_vm, get_active_env
    env = _FakeSSHEnv({})
    tokens = set_session_vars(platform="qqbot", session_key="agent:main:qqbot:dm:U1", session_id="sid-1")
    ssh_backend._active_environments[ssh_backend._resolve_container_task_id("sid-1")] = env
    cleanup_vm("sid-1")
    clear_session_vars(tokens)
    assert ssh_backend._active_environments == {"session:agent:main:qqbot:dm:U1": env}
    assert get_active_env("default") is None  # the pre-fix delivery lookup


def test_explicit_key_wins_over_stale_session_environment(ssh_backend, monkeypatch):
    env = _FakeSSHEnv({"/root/deliver/current.txt": b"current"})
    stale = _FakeSSHEnv({"/root/deliver/current.txt": b"wrong-session"})
    ssh_backend._active_environments["session:agent:main:qqbot:dm:U1"] = env
    ssh_backend._active_environments["session:stale"] = stale
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-id")
    monkeypatch.setenv("HERMES_SESSION_KEY", "stale")
    delivered = BasePlatformAdapter.filter_media_delivery_paths(
        [("/root/deliver/current.txt", False)], session_key="agent:main:qqbot:dm:U1")
    assert [Path(p).read_bytes() for p, _ in delivered] == [b"current"]
    assert not stale.fetched
