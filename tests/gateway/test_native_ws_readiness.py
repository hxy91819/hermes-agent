"""A ready native listener must not make its first client pay for cold WS imports.

The cron acceptance regression exposed synchronous schema imports during the first upgrade:
other profiles and concurrent cron clients could exhaust their handshake budgets.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

REPO = Path(__file__).resolve().parents[2]
PROBE = textwrap.dedent('''
    import asyncio, importlib.abc, importlib.machinery, json, sys, threading, types
    from websockets.asyncio.client import connect

    async def main():
        from gateway.run_api import start_gateway_api, stop_gateway_api
        from gateway.runtime_bootstrap import TicketStore
        assert 'tui_gateway.ws' not in sys.modules
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()

        class Loader(importlib.abc.Loader):
            def __init__(self, delegate): self.delegate = delegate
            def create_module(self, spec): return self.delegate.create_module(spec)
            def exec_module(self, module):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise RuntimeError('cold import blocked the owner event loop')
                self.delegate.exec_module(module)

        class Finder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                if fullname == 'tui_gateway.ws':
                    spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                    spec.loader = Loader(spec.loader)
                    return spec

        finder = Finder()
        sys.meta_path.insert(0, finder)
        home = sys.argv[1]
        tickets = TicketStore('owner', [home])
        authority = types.SimpleNamespace(db=types.SimpleNamespace(db_path=home+'/state.db'), profile_id=home)
        runner = types.SimpleNamespace(_draining=False, session_runtime_descriptor={'state':'ready'},
                                       session_authority=authority, session_ticket_store=tickets)
        starting = asyncio.create_task(start_gateway_api(runner))
        handle = None
        try:
            # Both readiness and the event loop must wait correctly for a slow real import.
            await asyncio.wait_for(entered.wait(), 2)
            assert not starting.done(), 'listener advertised readiness before its WS handler loaded'
            release.set()
            handle = await asyncio.wait_for(starting, 60)
            assert 'hermes_cli.web_server' not in sys.modules
            import tui_gateway.ws
            async def respond(ws, **kwargs):
                await ws.accept(subprotocol='hermes-gateway-v1')
                await ws.send_json({'ready': True})
                await ws.close()
            tui_gateway.ws.handle_ws = respond
            async def first_client():
                ticket = tickets.mint(profile_id=home, subject='human', purpose='interactive')
                protocols = ['hermes-gateway-v1', 'hermes-gateway-ticket.'+ticket]
                async with connect(handle.api_origin.replace('http:', 'ws:')+'/api/ws',
                                   subprotocols=protocols, open_timeout=2) as ws:
                    assert json.loads(await ws.recv()) == {'ready': True}
            await asyncio.gather(first_client(), first_client())
            print('RESULT '+json.dumps({'concurrent_first_clients':2}), file=sys.stderr)
        finally:
            release.set()
            sys.meta_path.remove(finder)
            if handle is None:
                try: handle = await starting
                except BaseException: pass
            if handle is not None: await stop_gateway_api(handle)

    asyncio.run(main())
''')


def test_slow_native_ws_initialization_precedes_readiness_and_keeps_loop_responsive(tmp_path):
    home = tmp_path / '.hermes'
    home.mkdir()
    env = {k:v for k,v in os.environ.items() if k in {'PATH','LANG','TZ','HERMES_RUNTIME_DIR','TMPDIR'}}
    env.update(HOME=str(tmp_path), HERMES_HOME=str(home), PYTHONPATH=str(REPO))
    proc = subprocess.run([sys.executable, '-c', PROBE, str(home)], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=90, check=False)
    lines = [line for line in (proc.stdout+proc.stderr).splitlines() if line.startswith('RESULT ')]
    assert proc.returncode == 0 and lines, proc.stderr[-4000:]
    assert json.loads(lines[-1][7:]) == {'concurrent_first_clients':2}
