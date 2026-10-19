"""Cooperate with the gateway's recoverable, per-profile session-repair lease."""
from contextlib import contextmanager
import logging
from pathlib import Path
import threading

logger = logging.getLogger(__name__)
_condition = threading.Condition()
_active = {}
_draining = set()


def _home(server, params):
    if params.get('profile'):
        try:
            return Path(server._profile_home(params['profile']) or server._launch_home()).resolve()
        except server.ProfileUnavailableError:
            return None  # Let each RPC preserve its existing unknown-profile contract.
    session = server._sessions.get(str(params.get('session_id') or ''))
    from tui_gateway.ws_legacy_fallback import connection_profile_home
    home = session.get('profile_home') if isinstance(session, dict) else connection_profile_home()
    return Path(home or server._launch_home()).resolve()


@contextmanager
def admission(server, params):
    from gateway.profile_repair_lease import lease_active
    home = _home(server, params)
    if home is None:
        yield True
        return
    with _condition:
        allowed = home not in _draining and not lease_active(home)
        if allowed:
            _active[home] = _active.get(home, 0) + 1
    try:
        yield allowed
    finally:
        if allowed:
            with _condition:
                _active[home] -= 1
                _condition.notify_all()


def _drain(server, home, stop, reclaimed):
    from gateway.profile_repair_lease import lease_active
    from hermes_state_registry import close_all_under
    from hermes_state_dbfile import release_idle_header_probe
    import gc

    with _condition:
        _draining.add(home)
    try:
        # No process-global lock is held while target requests/turns settle.
        with _condition:
            while _active.get(home, 0):
                if stop.is_set() or not lease_active(home):
                    return
                _condition.wait(.1)
        with server._sessions_lock:
            sessions = [(sid, s) for sid, s in server._sessions.items()
                        if Path(s.get('profile_home') or server._launch_home()).resolve() == home]
        for sid, session in sessions:
            server._interrupt_session_turn(sid, session)
        for _, session in sessions:
            for key in ('_run_thread', '_agent_build_thread'):
                thread = session.get(key)
                while thread is not None and thread.is_alive():
                    if stop.is_set() or not lease_active(home):
                        return  # The native holder check refuses repair; never close a live writer.
                    thread.join(.1)
            if session.get('_compute_host_turn_id'):
                return  # An isolated child still owns its writer; retry after it settles.
        if not lease_active(home):
            return
        for sid, session in sessions:
            if server._close_session_by_id(sid, end_reason='session_repair'):
                reclaimed.append({'session_id': sid, 'stored_session_id': str(session.get('session_key') or ''),
                                  'reason': 'session_repair'})
        close_all_under(home)
        if Path(server._launch_home()).resolve() == home:
            server._db = None
        gc.collect()
        release_idle_header_probe(home / 'state.db')
        return True
    finally:
        with _condition:
            _draining.discard(home)
            _condition.notify_all()


def start(server):
    """Watch only this backend's Hermes root; a dead CLI leaves no local stop marker."""
    from agent.memory_provider import spawn_context_thread
    from hermes_constants import get_default_hermes_root
    from gateway.profile_repair_lease import lease_active, _read
    root = get_default_hermes_root(home=server._launch_home()).resolve()
    stop = threading.Event()
    workers = {}
    completed = {}
    reclaimed = {}

    def watch():
        while not stop.wait(.2):
            for home in list(reclaimed):
                with _condition:
                    if home in _draining:
                        continue
                if lease_active(home):
                    continue
                from gateway.host_attach import host_gateway, invalidate_host_gateway_cache, profile_name_for_home
                invalidate_host_gateway_cache()
                owner = host_gateway()
                if owner is None or owner.home.resolve() != root or not owner.serves(profile_name_for_home(home)):
                    continue
                for payload in reclaimed.pop(home):
                    server._broadcast_global_event('session.reclaimed', payload)
            for marker in (root / 'profiles').glob('*/gateway.repair-lease.json'):
                home = marker.parent.resolve()
                if home.parent != (root / 'profiles').resolve():
                    continue
                lease = _read(home)
                if lease is None or completed.get(home) == lease['token']:
                    continue
                if not lease_active(home) or (home in workers and workers[home].is_alive()):
                    continue
                def run(target=home, token=lease['token']):
                    try:
                        with server._session_profile_runtime_scope({'profile_home': str(target)}, hydrate_secrets=False):
                            if _drain(server, target, stop, reclaimed.setdefault(target, [])):
                                completed[target] = token
                    except Exception:
                        logger.exception('Session repair drain failed for %s', target)
                worker = workers[home] = spawn_context_thread(run, name='profile-repair-drain')
                worker.start()
    thread = spawn_context_thread(watch, name='profile-repair-watch')
    thread.start()
    return stop
