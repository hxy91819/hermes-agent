"""Temporarily retire only a satellite Profile before repairing its session store."""
from pathlib import Path


def repair_with_profile_paused(db_path: Path, *, backup: bool):
    from gateway.host_attach import host_gateway, invalidate_host_gateway_cache, profile_name_for_home, launched_by_other_tenant
    from gateway.control_socket import request_unserve_profile, request_serve_profile_hot
    import os
    import uuid
    from gateway.status import get_process_start_time
    from hermes_state_repair import repair_state_db_schema

    home = db_path.parent
    invalidate_host_gateway_cache()
    owner = host_gateway()
    name = profile_name_for_home(home)
    # A launch Profile cannot be unserved by this protocol. Keep the native writer
    # refusal in that case; never turn a single-profile repair into a host stop.
    if owner is None or owner.standalone or launched_by_other_tenant(owner.home, home) or not owner.serves(name) or home.resolve() == owner.home.resolve():
        return repair_state_db_schema(db_path, backup=backup)
    lease = {"pid": os.getpid(), "start_time": get_process_start_time(os.getpid()), "token": uuid.uuid4().hex}

    report = {"repaired": False}
    stopped = False
    try:
        answer = request_unserve_profile(owner.home, name, repair_lease=lease)
        if not isinstance(answer, dict) or answer.get("unserved") != name or answer.get("error"):
            report["error"] = f"Profile stop was not confirmed: {answer}"
        else:
            # The DB's independent holder check remains authoritative: another CLI,
            # Desktop or an unretired turn must still prevent schema surgery.
            stopped = True
            report = repair_state_db_schema(db_path, backup=backup)
    finally:
        answer = request_serve_profile_hot(owner.home, name, repair_token=lease["token"])
        confirmed = isinstance(answer, dict) and answer.get("served") == name and not answer.get("error")
        if not confirmed:
            # A rescan may win the race after the marker is removed. Re-probe the
            # same owner rather than interpreting an error string as success.
            invalidate_host_gateway_cache()
            restored = host_gateway()
            confirmed = (restored is not None and restored.pid == owner.pid
                         and restored.home.resolve() == owner.home.resolve()
                         and restored.serves(name))
        if not confirmed:
            report["repaired"] = False
            report["error"] = f"{report.get('error') or 'Database repair finished'}; Profile restore was not confirmed: {answer}"
        elif stopped:
            from hermes_state_repair import _db_opens_cleanly
            reason = _db_opens_cleanly(db_path)
            if reason is None:
                # Serving opens SessionDB in the owner and can complete its native
                # recovery after retiring the old handles. Confirm from a fresh
                # external connection, never from the serve acknowledgement alone.
                if not report.get("repaired"):
                    report = {"repaired": True, "strategy": "profile-owner-recovery"}
            else:
                report["repaired"] = False
                report["error"] = f"{report.get('error') or 'Repair incomplete'}; Database remains unhealthy after Profile restore: {reason}"
    return report
