"""Lifecycle verbs must not acknowledge a stop/restart that maintenance will undo."""
import pytest

from tests.gateway.test_session_repair_serve_live import test_repair_with_dashboard_writer as repair_scenario

pytest_plugins = ['tests.gateway.test_multiplex_hot_serve_live']
pytestmark = [pytest.mark.platforms('linux'), pytest.mark.spawns_gateway_lookalike]


@pytest.mark.parametrize('action', ['stop', 'restart', 'repair'])
def test_lifecycle_during_two_process_repair(mux, tmp_path, peer, action):
    repair_scenario(mux, tmp_path, peer, 'success', action=action)
