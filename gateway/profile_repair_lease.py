"""Gateway-owned, crash-recoverable leases for temporary Profile maintenance.

Unlike gateway.parked, this state is not a user's persistent stop request. The
normal profile watcher retires it after the caller dies and the repair lock is free.
"""
import json
from pathlib import Path


def lease_path(home):
    return Path(home) / 'gateway.repair-lease.json'


def _repair_lock_free(home):
    from hermes_state_repair import _try_lock_nonblocking, _release_lock_handle
    try:
        handle = (Path(home) / 'state.db.repair.lock').open('a+b')
    except OSError:
        return False
    try:
        _try_lock_nonblocking(handle)
    except OSError:
        handle.close()
        return False
    _release_lock_handle(handle)
    return True


def _read(home):
    try:
        return json.loads(lease_path(home).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None


def lease_active(home):
    from gateway.status import _pid_exists, get_process_start_time
    lease = _read(home)
    if lease is None:
        return False
    pid = lease['pid']
    if _pid_exists(pid) and get_process_start_time(pid) in (None, lease['start_time']):
        return True
    if not _repair_lock_free(home):
        return True
    return False


def retire_expired_lease(home):
    # Only the owner calls this under its reconcile lock. Read-only enumerators
    # never unlink: a stale reader must not remove a newly issued lease.
    if not lease_active(home):
        lease_path(home).unlink(missing_ok=True)


def begin_lease(home, lease):
    from gateway.status import _pid_exists, get_process_start_time
    from hermes_cli.profiles import profile_is_parked
    if (not isinstance(lease, dict) or not isinstance(lease.get('pid'), int)
            or not isinstance(lease.get('token'), str) or not lease['token']
            or lease.get('start_time') is None or not _pid_exists(lease['pid'])
            or get_process_start_time(lease['pid']) != lease['start_time']):
        raise ValueError('repair caller identity is not live')
    if profile_is_parked(home) or lease_active(home):
        raise ValueError('Profile is stopped or already under maintenance')
    # Only the owner writes, under its reconcile lock. Atomic replacement also
    # leaves a complete record if the gateway itself crashes during persistence.
    from utils import atomic_write_text
    atomic_write_text(lease_path(home), json.dumps(lease))


def end_lease(home, token):
    lease = _read(home)
    if lease is None:
        return
    if lease.get('token') != token or not _repair_lock_free(home):
        raise ValueError('Profile maintenance is still owned by another repair')
    lease_path(home).unlink(missing_ok=True)
