"""Repair refusal must reach automation as failure without changing the damaged store."""
import argparse
from contextlib import closing
import sqlite3
import subprocess
import sys

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

