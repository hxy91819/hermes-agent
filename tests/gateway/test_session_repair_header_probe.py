"""A header-only descriptor must not masquerade as a live database writer."""
from pathlib import Path
import os

import pytest

from hermes_state import SessionDB
from hermes_state_dbfile import _pread_db_header, release_idle_header_probe
from hermes_cli.sqlite_safe_read import LiveConnectionError


def test_probe_is_released_only_after_all_connections_close(tmp_path):
    path = tmp_path / 'state.db'
    db = SessionDB(path)
    assert _pread_db_header(path, 16) == b'SQLite format 3\x00'
    with pytest.raises(LiveConnectionError):
        release_idle_header_probe(path)
    db.create_session('retained', source='test')
    db.close()
    release_idle_header_probe(path)
    if Path('/proc/self/fd').is_dir():
        for fd in Path('/proc/self/fd').iterdir():
            try:
                target = os.readlink(fd)
            except FileNotFoundError:
                continue
            assert target != str(path)
    with SessionDB(path) as reopened:
        assert reopened.get_session('retained')['id'] == 'retained'
