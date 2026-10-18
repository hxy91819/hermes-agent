"""Repair refusal must reach automation as failure without changing the damaged store."""
import argparse
from contextlib import closing
import sqlite3
import subprocess
import sys

import pytest

import hermes_state
from hermes_cli.sessions_cmd import cmd_sessions
from hermes_state import SessionDB
from hermes_state_repair import _db_opens_cleanly


def test_live_writer_repair_refusal_returns_failure(tmp_path, monkeypatch, capsys):
    path = tmp_path / "state.db"
    SessionDB(db_path=path).close()
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", path)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
         "c.execute('SELECT count(*) FROM sessions'); print('ready',flush=True); sys.stdin.readline()", str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        with closing(sqlite3.connect(path, isolation_level=None)) as conn:
            conn.execute("PRAGMA writable_schema=ON")
            conn.execute("INSERT INTO sqlite_master SELECT type,name,tbl_name,rootpage,sql "
                         "FROM sqlite_master WHERE name='idx_sessions_source'")
        assert _db_opens_cleanly(path) is not None
        result = cmd_sessions(argparse.Namespace(sessions_action="repair", check_only=False, no_backup=False))
        assert "Repair failed:" in capsys.readouterr().out
        assert _db_opens_cleanly(path) is not None
        assert result == 1
    finally:
        holder.communicate("\n", timeout=10)



@pytest.mark.parametrize("release, resume_ack", [(True, True), (False, True), (True, False), (True, "rescan")])
def test_profile_repair_releases_only_target_and_restores_it(tmp_path, monkeypatch, release, resume_ack):
    from gateway import control_socket, host_attach
    from hermes_cli.sessions_repair_owner import repair_with_profile_paused

    root = tmp_path / ".hermes"
    home = root / "profiles" / "support"
    home.mkdir(parents=True)
    path = home / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("kept", "cli")
    db.append_message("kept", "user", "keep my conversation")
    db.close()
    other = SessionDB(db_path=tmp_path / "other.db")
    other.create_session("other", "cli")
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
         "c.execute('SELECT count(*) FROM sessions'); print('ready',flush=True); sys.stdin.readline()", str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    assert holder.stdout.readline().strip() == "ready"
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("INSERT INTO sqlite_master SELECT type,name,tbl_name,rootpage,sql "
                     "FROM sqlite_master WHERE name='idx_sessions_source'")
    owner = host_attach.HostGateway(pid=1, home=root, profiles=("default", "support", "other"))
    owner_seen = [owner]
    monkeypatch.setattr(host_attach, "host_gateway", lambda: owner_seen[0])
    monkeypatch.setattr(host_attach, "profile_name_for_home", lambda home: "support")
    calls = []

    def stop(host, name):
        assert host == root and name == "support"
        assert (home / "gateway.parked").exists()
        if release:
            holder.communicate("\n", timeout=10)
        calls.append("stop")
        return {"unserved": name}

    def start(host, name):
        assert host == root and name == "support"
        assert not (home / "gateway.parked").exists()
        if release:
            with SessionDB(db_path=path) as resumed:
                resumed.append_message("kept", "assistant", "conversation continues")
        other.append_message("other", "assistant", "uninterrupted")
        calls.append("start")
        if not resume_ack:
            owner_seen[0] = None
        return {"served": name} if resume_ack is True else {"error": "already served"} if resume_ack else {"pending": True}

    monkeypatch.setattr(control_socket, "request_unserve_profile", stop)
    monkeypatch.setattr(control_socket, "request_serve_profile_hot", start)
    try:
        report = repair_with_profile_paused(path, backup=True)
        assert report["repaired"] is bool(release and resume_ack), report
        assert calls == ["stop", "start"]
        if not release:
            assert _db_opens_cleanly(path) is not None
            return
        assert _db_opens_cleanly(path) is None
        with closing(sqlite3.connect(path, isolation_level=None)) as conn:
            assert conn.execute("SELECT content FROM messages ORDER BY id").fetchall() == [
                ("keep my conversation",), ("conversation continues",)]
    finally:
        if holder.poll() is None:
            holder.communicate("\n", timeout=10)
        other.close()


def test_other_root_same_name_never_stops_profile(tmp_path, monkeypatch):
    from gateway import host_attach, control_socket
    from hermes_cli.sessions_repair_owner import repair_with_profile_paused
    home = tmp_path / 'tenant-a' / 'profiles' / 'support'
    home.mkdir(parents=True)
    path = home / 'state.db'
    SessionDB(db_path=path).close()
    owner = host_attach.HostGateway(pid=1, home=tmp_path / 'tenant-b', profiles=('support',))
    monkeypatch.setattr(host_attach, 'host_gateway', lambda: owner)
    monkeypatch.setattr(host_attach, 'profile_name_for_home', lambda home: 'support')
    def forbidden(*args):
        pytest.fail('must not alter another tenant')
    monkeypatch.setattr(control_socket, 'request_unserve_profile', forbidden)
    monkeypatch.setattr(control_socket, 'request_serve_profile_hot', forbidden)
    assert repair_with_profile_paused(path, backup=True)['repaired']
