"""Shared gateway plus the separate dashboard backend own the same profile store."""
import asyncio
import json
import re
import sqlite3
import subprocess
import sys
import queue
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from websockets.asyncio.client import connect

from tests.gateway.test_multiplex_hot_serve_live import ROOT, start_daemon, stop, control, served_homes
from tests.waiting import wait_until

pytest_plugins = ['tests.gateway.test_multiplex_hot_serve_live']
pytestmark = [pytest.mark.platforms('linux'), pytest.mark.spawns_gateway_lookalike]


async def turn(ws, profile, sid=None):
    async def rpc(method, **params):
        await ws.send(json.dumps({'jsonrpc': '2.0', 'id': method, 'method': method, 'params': params}))
        async with asyncio.timeout(60):
            while True:
                msg = json.loads(await ws.recv())
                if msg.get('id') == method:
                    assert 'result' in msg, msg
                    return msg['result']
    result = await rpc('session.resume', session_id=sid, profile=profile) if sid else await rpc('session.create', profile=profile)
    runtime_sid = result['session_id']
    await rpc('prompt.submit', session_id=runtime_sid, text='WS_SHARED dashboard-repair')
    async with asyncio.timeout(60):
        while True:
            msg = json.loads(await ws.recv())
            params = msg.get('params', {})
            if msg.get('method') == 'event' and params.get('type') == 'message.complete':
                assert 'LOCAL_ACK_WS_SHARED' in params['payload']['text'], msg
                return result.get('stored_session_id') or runtime_sid


async def dashboard_turn(url, profile, sid=None):
    async with connect(url) as ws:
        return await turn(ws, profile, sid)


async def held_dashboard_session(url, ready):
    # Keep the actual client connected: disconnected sessions can be reaped
    # before repair, hiding the independent backend's retained AIAgent writer.
    async with connect(url) as ws:
        stored_sid = await turn(ws, 'boot')
        ready.put(stored_sid)
        async with asyncio.timeout(90):
            while True:
                event = json.loads(await ws.recv()).get('params', {})
                if event.get('type') == 'session.reclaimed':
                    payload = event.get('payload', {})
                    if payload.get('stored_session_id') == stored_sid:
                        return payload


@pytest.mark.parametrize('outcome', ['success', 'exception', 'killed', 'unserved', 'failed-startup'])
def test_repair_with_dashboard_writer(mux, tmp_path, outcome):
    gateway, desc = start_daemon(mux)
    backend = repair = None
    observer_pool = ThreadPoolExecutor(max_workers=1)
    log_path = tmp_path / 'serve.log'
    try:
        with log_path.open('w') as log:
            backend = subprocess.Popen([sys.executable, '-c',
                'from hermes_cli.web_server import start_server; start_server(port=0, headless=True, open_browser=False, ssh_session_token="loopback-repair-test")'],
                cwd=ROOT, env=mux['env'], stdout=log, stderr=subprocess.STDOUT)
        wait_until(lambda: 'HERMES_BACKEND_READY port=' in log_path.read_text(), timeout=60, what='dashboard ready')
        port = re.search(r'HERMES_BACKEND_READY port=(\d+)', log_path.read_text()).group(1)
        url = f'ws://127.0.0.1:{port}/api/ws?token=loopback-repair-test'
        ready = queue.Queue()
        observed = observer_pool.submit(asyncio.run, held_dashboard_session(url, ready))
        sid = ready.get(timeout=60)
        other = asyncio.run(dashboard_turn(url, 'default'))
        with closing(sqlite3.connect(mux['boot'] / 'state.db', isolation_level=None)) as db:
            db.execute('PRAGMA writable_schema=ON')
            db.execute("INSERT INTO sqlite_master SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master WHERE name='idx_sessions_source'")
        if outcome in ('unserved', 'failed-startup'):
            control(mux['root'], 'unserve-profile', {'name': 'boot'})
            if outcome == 'failed-startup':
                failed = control(mux['root'], 'serve-profile', {'name': 'boot'})
                assert 'error' in failed, failed
            assert mux['boot'].resolve() not in served_homes(control(mux['root'], 'identify'))
        # Hold the repair boundary after both processes have drained. The peer must
        # complete a real turn while the target is unavailable.
        code = """
import sys,time
from pathlib import Path
import hermes_state_repair as repair
native = repair.repair_state_db_schema
home = Path(sys.argv[1])
outcome = sys.argv[2]
def held(path, *, backup):
    with repair._cross_process_repair_lock(path) as locked:
        assert locked
        (home / 'repair-ready').touch()
        while not (home / 'repair-release').exists():
            time.sleep(.05)
        if outcome == 'exception':
            raise RuntimeError('injected repair failure')
    return native(path, backup=backup)
repair.repair_state_db_schema = held
sys.argv = ['hermes', '-p', 'boot', 'sessions', 'repair']
from hermes_cli.main import main
main()
"""
        repair = subprocess.Popen([sys.executable, '-c', code, str(mux['boot']), outcome],
            cwd=ROOT, env=mux['env'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        wait_until((mux['boot'] / 'repair-ready').exists, timeout=45, what='both writers drained')
        from hermes_state_holders import foreign_state_db_holders
        assert not foreign_state_db_holders(mux['boot'] / 'state.db')
        assert mux['boot'].resolve() not in served_homes(control(mux['root'], 'identify'))
        asyncio.run(dashboard_turn(url, 'default', other))
        if outcome == 'killed':
            repair.kill()
        else:
            (mux['boot'] / 'repair-release').touch()
        stdout, stderr = repair.communicate(timeout=60)
        if outcome in ('success', 'unserved', 'failed-startup'):
            assert repair.returncode == 0, stdout + stderr
        else:
            assert repair.returncode != 0
            if outcome == 'exception':
                assert 'injected repair failure' in stderr
        wait_until(lambda: mux['boot'].resolve() in served_homes(control(mux['root'], 'identify')),
                   timeout=50, what='target automatically restored')
        assert observed.result(timeout=15)['reason'] == 'session_repair'
        assert not (mux['boot'] / 'gateway.parked').exists()
        assert not (mux['boot'] / 'gateway.repair-lease.json').exists()
        assert control(mux['root'], 'identify')['instance_id'] == desc['instance_id']
        assert gateway.poll() is None and backend.poll() is None
        asyncio.run(dashboard_turn(url, 'boot', sid))
        asyncio.run(dashboard_turn(url, 'default', other))
        with closing(sqlite3.connect(mux['boot'] / 'state.db')) as db:
            assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    finally:
        if repair is not None and repair.poll() is None:
            repair.kill()
            repair.wait(timeout=10)
        if backend is not None:
            stop(backend, expect=None)
        stop(gateway, expect=None)
        observer_pool.shutdown(wait=True, cancel_futures=True)
