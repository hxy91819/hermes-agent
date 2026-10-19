"""Cooperate with the gateway's recoverable, per-profile session-repair lease."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
from pathlib import Path
import threading

logger = logging.getLogger(__name__)
_condition = threading.Condition()
_active = {}
_draining = {}
_cancel_check = ContextVar('session_repair_cancel_check', default=None)


class DrainCancelled(BaseException):
    """Unwind repair cleanup even through best-effort plugin exception handlers."""


def checkpoint():
    check = _cancel_check.get()
    if check is not None and not check():
        raise DrainCancelled()


def _owns_lease(home, token):
    from gateway.profile_repair_lease import lease_active
    return token is not None and lease_active(home, token=token)


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
        allowed = not lease_active(home)
        if allowed:
            _active[home] = _active.get(home, 0) + 1
    try:
        yield allowed
    finally:
        if allowed:
            with _condition:
                _active[home] -= 1
                _condition.notify_all()


def _drain(server, home, stop, reclaimed, token=None):
    from agent.memory_provider import spawn_context_thread
    from gateway.profile_repair_lease import _read
    lease = _read(home)
    token = token or (lease['token'] if lease else None)
    def valid():
        return not stop.is_set() and _owns_lease(home, token)
    if not valid():
        return False
    done = threading.Event()
    result = []

    def cleanup():
        scope = _cancel_check.set(valid)
        try:
            result.append(_drain_sessions(server, home, reclaimed))
        except DrainCancelled:
            pass
        except Exception:
            logger.exception('Session repair drain failed for %s', home)
        finally:
            _cancel_check.reset(scope)
            done.set()

    with _condition:
        _draining[home] = token
    worker = spawn_context_thread(cleanup, name='profile-repair-cleanup')
    worker.start()
    try:
        # Python cannot kill a blocked plugin thread. Retire its authority instead;
        # checkpoints fence every continuation when the plugin eventually returns.
        while not done.wait(.1):
            if not valid():
                return False
        return bool(result and result[0] and valid())
    finally:
        with _condition:
            if _draining.get(home) == token:
                _draining.pop(home)
            _condition.notify_all()


def _drain_sessions(server, home, reclaimed):
    from hermes_state_dbfile import release_idle_header_probe
    with _condition:
        while _active.get(home, 0):
            checkpoint()
            _condition.wait(.1)
    checkpoint()
    with server._sessions_lock:
        sessions = [(sid, s) for sid, s in server._sessions.items()
                    if Path(s.get('profile_home') or server._launch_home()).resolve() == home]
    for sid, session in sessions:
        checkpoint()
        server._interrupt_session_turn(sid, session)
    for _, session in sessions:
        for key in ('_run_thread', '_agent_build_thread'):
            thread = session.get(key)
            while thread is not None and thread.is_alive():
                checkpoint()
                thread.join(.1)
        if session.get('_compute_host_turn_id'):
            return False  # The native holder check must still refuse an isolated live writer.
    for sid, session in sessions:
        checkpoint()
        # Record before a potentially blocking end hook, so cancellation can still
        # notify the connected client after the owner restores the profile.
        reclaimed.append({'session_id': sid, 'stored_session_id': str(session.get('session_key') or ''),
                          'reason': 'session_repair'})
        server._close_session_by_id(sid, end_reason='session_repair')
        checkpoint()
    # Fence selection with registry acquisition: a stale cleanup cannot select
    # a recovered generation, even if the lease ends just before the sweep.
    from hermes_state_registry import close_all_under
    @contextmanager
    def selection_guard():
        with _condition:
            checkpoint()
            if Path(server._launch_home()).resolve() == home:
                server._db = None
            yield

    close_all_under(home, selection_guard=selection_guard)
    checkpoint()
    release_idle_header_probe(home / 'state.db')
    return True


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
                            if _drain(server, target, stop, reclaimed.setdefault(target, []), token):
                                completed[target] = token
                    except Exception:
                        logger.exception('Session repair drain failed for %s', target)
                worker = workers[home] = spawn_context_thread(run, name='profile-repair-drain')
                worker.start()
    thread = spawn_context_thread(watch, name='profile-repair-watch')
    thread.start()
    return stop
