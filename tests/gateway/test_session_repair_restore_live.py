"""Repair restores a Profile even when a background reader observes maintenance."""
import pytest

from tests.gateway.test_session_repair_live import (
    test_repair_live_profile_preserves_host_and_other_profile as repair_scenario,
)

pytestmark = [pytest.mark.platforms("linux"), pytest.mark.spawns_gateway_lookalike]

pytest_plugins = ["tests.gateway.test_multiplex_hot_serve_live"]


def test_repair_restores_after_maintenance_store_probe(mux, tmp_path):
    # Pin the interleaving: a background reader reaches the retired store while
    # the repair lease is still active. Use the real opener and its real refusal.
    injected = tmp_path / 'startup'
    injected.mkdir()
    (injected / 'sitecustomize.py').write_text('''
from gateway.run_profile_reconcile import GatewayProfileReconcileMixin
native = GatewayProfileReconcileMixin._unserve_profile
async def unserve(self, name, home):
    await native(self, name, home)
    from gateway.run import _profile_runtime_scope
    from gateway.profile_repair_lease import lease_active
    if lease_active(home):
        with _profile_runtime_scope(home, hydrate_secrets=False):
            for cache in (self.session_store._db_handle_cache, self._session_db_handle_cache):
                cache._initial_retry_delay = cache._max_retry_delay = 60
            self.session_store._db
            self._session_db
GatewayProfileReconcileMixin._unserve_profile = unserve
''')
    mux['env']['PYTHONPATH'] = str(injected) + ':' + mux['env']['PYTHONPATH']
    repair_scenario(mux)
