"""Abandoned cleanup cannot retain admission or act on a recovered runtime."""
from contextlib import nullcontext
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from gateway.profile_repair_lease import begin_lease, lease_active, retire_expired_lease
from gateway.status import get_process_start_time
from tests.waiting import wait_until
from tui_gateway.profile_repair import _drain, admission


def caller_lease(home, token):
    caller = subprocess.Popen([sys.executable, '-c', 'import sys; print("ready", flush=True); sys.stdin.readline()'],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert caller.stdout.readline().strip() == 'ready'
    begin_lease(home, {'pid': caller.pid, 'start_time': get_process_start_time(caller.pid), 'token': token})
    return caller


def kill_caller(caller):
    if caller.poll() is None:
        caller.kill()
    caller.wait(timeout=5)
    caller.stdin.close()
    caller.stdout.close()


def test_old_worker_cannot_continue_under_replacement_lease(tmp_path):
    home = tmp_path.resolve()
    entered, release = threading.Event(), threading.Event()
    closed = []

    def teardown(sid, *, end_reason):
        closed.append(sid)
        if sid == 'old':
            entered.set()
            assert release.wait(15)
        return True

    server = SimpleNamespace(_sessions={'old': {'profile_home': str(home)}, 'next': {'profile_home': str(home)}},
        _sessions_lock=threading.Lock(), _launch_home=lambda: str(home),
        _interrupt_session_turn=lambda *a: None, _close_session_by_id=teardown)
    caller = caller_lease(home, 'old-token')
    worker = threading.Thread(target=_drain, args=(server, home, threading.Event(), []))
    worker.start()
    try:
        assert entered.wait(5)
        kill_caller(caller)
        assert not lease_active(home)
        retire_expired_lease(home)
        worker.join(timeout=5)
        assert not worker.is_alive(), 'drain supervisor must finish while teardown is still blocked'
        with admission(server, {'session_id': 'next'}) as allowed:
            assert allowed
        # A new caller has a different token; lease_active alone would let the old
        # cleanup continue into the next session and a path-wide connection sweep.
        caller = caller_lease(home, 'replacement-token')
        release.set()
        wait_until(lambda: not any(t.name == 'profile-repair-cleanup' and t.is_alive()
                                  for t in threading.enumerate()), timeout=5, what='old cleanup cancelled')
        assert closed == ['old']
        assert lease_active(home)
    finally:
        release.set()
        worker.join(timeout=5)
        kill_caller(caller)
        retire_expired_lease(home)


@pytest.mark.parametrize('stage', ['end-hook', 'memory-commit', 'agent-close'])
def test_cancelled_finalizer_releases_only_its_old_reference(tmp_path, monkeypatch, stage):
    from run_agent import AIAgent
    from hermes_state_registry import acquire, release
    from hermes_cli import lifecycle
    from tui_gateway import server

    home = tmp_path.resolve()
    entered, unblock, finished = threading.Event(), threading.Event(), threading.Event()
    old_db = acquire(home / 'state.db')
    old_db.create_session('retained', source='tui')
    agent = AIAgent.__new__(AIAgent)
    agent.session_id, agent._session_db, agent._owns_session_db = 'retained', old_db, True
    agent._session_messages = []

    def block():
        entered.set()
        assert unblock.wait(15)

    monkeypatch.setattr(lifecycle, 'invoke_hook', lambda *a, **kw: block() if stage == 'end-hook' else None)
    agent.commit_memory_session = lambda *a: block() if stage == 'memory-commit' else None
    agent.shutdown_memory_provider = lambda *a: block() if stage == 'agent-close' else None
    for name in ('_close_task_resources', '_close_active_children', '_drop_shared_client', '_close_request_clients',
                 '_close_codex_session', '_trim_process_memory'):
        setattr(agent, name, lambda *a, **kw: None)
    # The real finalizer may open a scoped DB before close; cancellation must
    # prevent further lifecycle effects once a new runtime is admitted.
    monkeypatch.setattr(server, '_session_db', lambda s: nullcontext(old_db))
    for name in ('_lock_vault_managers', '_release_active_session_slot', '_interrupt_session_turn',
                 '_announce_cancelled_gateway_approvals'):
        monkeypatch.setattr(server, name, lambda *a, **kw: None)
    monkeypatch.setattr(server, '_notify_session_boundary', lambda *a: [])
    monkeypatch.setattr(server, '_session_profile_runtime_scope', lambda *a, **kw: nullcontext())
    monkeypatch.setattr(server, '_launch_home', lambda: str(home.parent))
    monkeypatch.setattr(server, '_profile_home', lambda name: str(home))
    session = {'profile_home': str(home), 'session_key': 'retained', 'agent': agent,
               'history': [{'role': 'user', 'content': 'retained'}], 'history_lock': threading.Lock()}
    monkeypatch.setattr(server, '_sessions', {'old': session})
    caller = caller_lease(home, 'finalizer-token')

    def drain():
        try:
            _drain(server, home, threading.Event(), [])
        finally:
            finished.set()

    worker = threading.Thread(target=drain)
    worker.start()
    restored = None
    try:
        assert entered.wait(5)
        kill_caller(caller)
        retire_expired_lease(home)
        assert finished.wait(5), 'blocked plugin must not retain the local pause'
        with admission(server, {'profile': 'target'}) as allowed:
            assert allowed
        restored = acquire(home / 'state.db')
        restored.create_session('new-runtime', source='tui')
        unblock.set()
        wait_until(lambda: not agent._owns_session_db, timeout=5, what='old reference released')
        restored.create_session('after-old-cleanup', source='tui')
        assert restored.get_session('new-runtime') is not None
        assert old_db.get_session('retained')['end_reason'] != 'agent_close'
    finally:
        unblock.set()
        worker.join(timeout=5)
        kill_caller(caller)
        retire_expired_lease(home)
        if restored is not None:
            release(restored)
        if agent._owns_session_db:
            release(old_db)


def test_lease_expires_before_registry_sweep(tmp_path, monkeypatch):
    import hermes_state_registry as registry
    home = tmp_path.resolve()
    entered, unblock = threading.Event(), threading.Event()
    sweep = registry.close_all_under

    def held_sweep(directory, **kwargs):
        entered.set()
        assert unblock.wait(15)
        return sweep(directory, **kwargs)

    monkeypatch.setattr(registry, 'close_all_under', held_sweep)
    server = SimpleNamespace(_sessions={}, _sessions_lock=threading.Lock(), _launch_home=lambda: str(home))
    caller = caller_lease(home, 'sweep-token')
    worker = threading.Thread(target=_drain, args=(server, home, threading.Event(), []))
    worker.start()
    restored = None
    try:
        assert entered.wait(5)
        kill_caller(caller)
        retire_expired_lease(home)
        worker.join(timeout=5)
        assert not worker.is_alive()
        restored = registry.acquire(home / 'state.db')
        restored.create_session('recovered', source='tui')
        unblock.set()
        wait_until(lambda: not any(t.name == 'profile-repair-cleanup' and t.is_alive()
                                  for t in threading.enumerate()), timeout=5, what='stale sweep cancelled')
        restored.create_session('still-open', source='tui')
        assert restored.get_session('recovered') is not None
    finally:
        unblock.set()
        worker.join(timeout=5)
        kill_caller(caller)
        retire_expired_lease(home)
        if restored is not None:
            registry.release(restored)
