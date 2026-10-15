"""Remote callback reads share the lifecycle guard's content limits."""

from __future__ import annotations


def read_remote_reference(reader, path: str, max_bytes: int, *, remote_only: bool):
    from cron.lifecycle_guard import _sanitize_remote_script_text

    if remote_only:
        try:
            text = reader(path, max_bytes=max_bytes)
        except Exception:  # health: allow BLE001 -- callback failures return an explicit unsafe verdict
            return None, True
    else:
        text = reader(path)
    return _sanitize_remote_script_text(text, max_bytes=max_bytes)
