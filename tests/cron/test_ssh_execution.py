"""Exercise cron dispatch with real imports and a local SSH transport boundary."""
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from cron import ssh_execution as remote
from cron.lifecycle_guard import GatewayLifecycleBlocked, check_gateway_lifecycle
from cron.scheduler_script import _run_job_script as _run_script


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'
    (root / 'scripts').mkdir(parents=True)
    config = {
        'ssh_adaptation': {'enabled': True},
        'terminal': {'backend': 'ssh', 'ssh_host': 'fixture', 'ssh_user': 'fixture', 'ssh_key': ''},
    }
    monkeypatch.setattr('hermes_cli.config.load_config', lambda: config)

    class Transport:
        def get_temp_dir(self):
            return "/workspace"

        def _run_ssh(self, command, timeout=70):
            return subprocess.run(
                ['bash', '-c', command.replace('/workspace', str(root))],
                capture_output=True, text=True, timeout=timeout, check=False,
            )

    monkeypatch.setattr(remote, 'ssh_environment', lambda **kwargs: Transport())
    return root, config


def test_dispatch_stdout_interpreter_and_remote_cwd(workbench):
    root, _ = workbench
    (root / 'scripts/task.py').write_text("import sys; print(sys.executable); print('diagnostic', file=sys.stderr)")
    (root / 'scripts/alias.sh').symlink_to('task.py')
    ok, out = _run_script('alias.sh', interpreter=sys.executable)
    assert ok and out == sys.executable, (ok, out)
    (root / 'scripts/task.sh').write_text('pwd; echo remote-result')
    (root / 'sub').mkdir()
    ok, out = _run_script('task.sh', workdir='/workspace/sub')
    assert ok and out == str(root / 'sub') + '\nremote-result'
    assert not _run_script('task.py', interpreter='/missing/python3')[0]
    assert not _run_script('task.sh', workdir='/workspace/missing')[0]


def test_guard_uses_execution_cwd_and_never_falls_back(workbench):
    root, config = workbench
    (root / 'scripts/main.sh').write_text('bash helper')
    (root / 'scripts/helper').write_text('echo wrong-directory')
    (root / 'helper').write_text('hermes gateway restart')
    with pytest.raises(GatewayLifecycleBlocked):
        check_gateway_lifecycle('', 'main.sh', workdir='/workspace')
    (root / 'helper').write_text('echo safe')
    check_gateway_lifecycle('', 'main.sh', workdir='/workspace')
    (root / 'helper').unlink()
    with pytest.raises(GatewayLifecycleBlocked):
        check_gateway_lifecycle('', 'main.sh', workdir='/workspace')
    config['terminal']['backend'] = 'local'
    assert _run_script('main.sh')[0] is False


@pytest.mark.platforms('linux')
def test_cancel_stops_remote_process_group(workbench):
    root, _ = workbench
    (root / 'scripts/slow.py').write_text(
        "import time; from pathlib import Path; Path('started').write_text('yes'); "
        "time.sleep(2); Path('late').write_text('bad')"
    )
    event = threading.Event()
    timer = threading.Timer(.5, event.set)
    timer.start()
    try:
        ok, _ = _run_script('slow.py', cancel_event=event)
    finally:
        timer.cancel()
    assert not ok and (root / 'started').exists()
    # health: allow HX013 -- observe beyond the cancelled writer's deadline to prove it cannot write late
    threading.Event().wait(2)
    assert not (root / 'late').exists()
