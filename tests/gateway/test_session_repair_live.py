"""Repair a damaged satellite under a real shared gateway without restarting its peers."""
import asyncio
import json
from contextlib import closing
import sqlite3
import subprocess
import sys

import pytest

from tests.gateway.test_multiplex_hot_serve_live import (
    ROOT, control, open_session_for, served_homes, start_daemon, stop, tail,
)

pytest_plugins = ["tests.gateway.test_multiplex_hot_serve_live"]
pytestmark = [pytest.mark.platforms("linux"), pytest.mark.spawns_gateway_lookalike]


async def continue_session(root, home, desc, sid, turn):
    from websockets.asyncio.client import connect
    from tests.gateway.test_unified_gateway_native_live import ok
    grant = control(root, 'session-ticket', {'profile_id': str(home.resolve()),
                    'instance_id': desc['instance_id'], 'purpose': 'interactive'})
    url = desc['api_origin'].replace('http:', 'ws:') + '/api/ws'
    async with connect(url, subprotocols=['hermes-gateway-v1', 'hermes-gateway-ticket.' + grant['ticket']]) as ws:
        await ok(ws, 'session.resume', session_id=sid)
        await ok(ws, 'prompt.submit', session_id=sid, input_id=turn, text='WS_SHARED ' + turn)
        async with asyncio.timeout(60):
            while True:
                event = json.loads(await ws.recv())
                params = event.get('params', {})
                if event.get('method') == 'event' and params.get('type') == 'message.complete':
                    assert 'LOCAL_ACK_WS_SHARED' in params['payload']['text'], event
                    return


def test_repair_live_profile_preserves_host_and_other_profile(mux):
    root, home = mux["root"], mux["boot"]
    proc, desc = start_daemon(mux)
    try:
        from tests.waiting import wait_until
        wait_until(lambda: "kanban dispatcher: embedded" in tail(mux), timeout=30, interval=.1, what="startup chores completed")
        target_sid = asyncio.run(open_session_for(root, home, desc))
        other_sid = asyncio.run(open_session_for(root, root, desc))
        asyncio.run(continue_session(root, home, desc, target_sid, 'before-repair'))
        asyncio.run(continue_session(root, root, desc, other_sid, 'peer-before'))
        with closing(sqlite3.connect(home / "state.db", isolation_level=None)) as db:
            db.execute("PRAGMA writable_schema=ON")
            db.execute("INSERT INTO sqlite_master SELECT type,name,tbl_name,rootpage,sql "
                       "FROM sqlite_master WHERE name='idx_sessions_source'")
        repaired = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "-p", "boot", "sessions", "repair"],
            cwd=ROOT, env=mux["env"], capture_output=True, text=True, timeout=90, check=False,
        )
        assert repaired.returncode == 0, repaired.stdout + repaired.stderr + tail(mux)
        assert "Repaired" in repaired.stdout, repaired.stdout
        after = control(root, "identify")
        assert after["instance_id"] == desc["instance_id"]
        assert proc.poll() is None
        assert served_homes(after) == {root.resolve(), home.resolve()}
        for directory, sid in ((home, target_sid), (root, other_sid)):
            with closing(sqlite3.connect(directory / "state.db")) as db:
                assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                assert db.execute("SELECT id FROM sessions WHERE id=?", (sid,)).fetchone() == (sid,)
            assert asyncio.run(open_session_for(root, directory, after))
            asyncio.run(continue_session(root, directory, after, sid, 'after-repair'))
            with closing(sqlite3.connect(directory / 'state.db')) as db:
                texts = [row[0] for row in db.execute('SELECT content FROM messages WHERE session_id=?', (sid,))]
                assert any('after-repair' in text for text in texts)
                assert any('before' in text for text in texts)
    finally:
        stop(proc, expect=None)


def test_store_recovery_keeps_shared_loop_responsive(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    from gateway import run_runtime, session_authority
    from hermes_constants import get_hermes_home

    home = tmp_path.resolve()
    opened, release = threading.Event(), threading.Event()
    observed = []

    class Runner:
        session_runtime_descriptor = {'instance_id': 'test'}
        session_authorities = SimpleNamespace(add=lambda *a, **k: None, replace=lambda *a: None)

        @property
        def _session_db(self):
            observed.append(get_hermes_home().resolve())
            opened.set()
            assert release.wait(5), 'shared loop blocked while recovering store'
            return SimpleNamespace(db_path=home / 'state.db')

    async def initialize(*args, **kwargs):
        return object()

    monkeypatch.setattr(session_authority, 'initialize_session_authority', initialize)

    async def exercise():
        task = asyncio.create_task(run_runtime._build_profile_authority(Runner(), 'target', home, register=False))
        try:
            from tests.waiting import async_wait_until
            await async_wait_until(opened.is_set, timeout=3, what="store recovery started")
            # This coroutine represents an unrelated Profile's ready work.
            release.set()
            await task
        finally:
            release.set()

    asyncio.run(exercise())
    assert observed == [home]


def test_killed_repair_is_automatically_unserved_then_restored(mux):
    from tests.waiting import wait_until
    root, home = mux['root'], mux['boot']
    proc, desc = start_daemon(mux)
    repair = None
    ready = home / 'repair-test-ready'
    try:
        sid = asyncio.run(open_session_for(root, home, desc))
        code = '''
import sys,time
from pathlib import Path
import hermes_state_repair
from hermes_cli.sessions_repair_owner import repair_with_profile_paused
path = Path(sys.argv[1])
def blocked(path, *, backup):
    with hermes_state_repair._cross_process_repair_lock(path) as locked:
        assert locked
        path.with_name('repair-test-ready').touch()
        sys.stdin.readline()
hermes_state_repair.repair_state_db_schema = blocked
repair_with_profile_paused(path, backup=True)
'''
        repair = subprocess.Popen([sys.executable, '-c', code, str(home / 'state.db')],
                                  cwd=ROOT, env=mux['env'], stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_until(ready.exists, timeout=25, what='repair holds lock after target unserved')
        assert not (home / 'gateway.parked').exists()
        assert home.resolve() not in served_homes(control(root, 'identify'))
        assert root.resolve() in served_homes(control(root, 'identify'))
        repair.kill()
        repair.wait(timeout=10)
        # No explicit rescan/serve: the existing watcher recovers the gateway-owned lease.
        wait_until(lambda: home.resolve() in served_homes(control(root, 'identify')),
                   timeout=50, interval=.2, what='dead repair caller automatically restored')
        after = control(root, 'identify')
        assert after['instance_id'] == desc['instance_id']
        assert not (home / 'gateway.repair-lease.json').exists()
        asyncio.run(continue_session(root, home, after, sid, 'after-killed-repair'))
    finally:
        if repair is not None and repair.poll() is None:
            repair.kill()
            repair.wait(timeout=10)
        stop(proc, expect=None)


@pytest.mark.parametrize("multiplex", [False, True])
def test_launch_profile_repair_refuses_without_stopping_gateway(mux, multiplex):
    root = mux['root']
    mux["config"]["gateway"]["multiplex_profiles"] = multiplex
    (root / "config.yaml").write_text(json.dumps(mux["config"]))
    proc, desc = start_daemon(mux)
    try:
        sid = asyncio.run(open_session_for(root, root, desc))
        asyncio.run(continue_session(root, root, desc, sid, 'main-before-repair'))
        with closing(sqlite3.connect(root / 'state.db', isolation_level=None)) as db:
            db.execute('PRAGMA writable_schema=ON')
            db.execute("INSERT INTO sqlite_master SELECT type,name,tbl_name,rootpage,sql "
                       "FROM sqlite_master WHERE name='idx_sessions_source'")
        result = subprocess.run([sys.executable, '-m', 'hermes_cli.main', '-p', 'default', 'sessions', 'repair'],
                                cwd=ROOT, env=mux['env'], capture_output=True, text=True, timeout=90, check=False)
        assert result.returncode == 1, result.stdout
        assert 'live writer still holds state.db' in result.stdout
        assert control(root, 'identify')['instance_id'] == desc['instance_id']
        assert proc.poll() is None
        assert not (root / 'gateway.repair-lease.json').exists()
        with closing(sqlite3.connect(root / 'state.db')) as db:
            with pytest.raises(sqlite3.DatabaseError, match='already exists'):
                db.execute('PRAGMA integrity_check').fetchall()
    finally:
        stop(proc, expect=None)
