"""Shared SSH workbenches must not roll one profile's skills back from another."""
import io
import shutil
import tarfile
from contextlib import contextmanager
from pathlib import Path

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.environments.ssh import SSHEnvironment


@contextmanager
def profile(home):
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def test_clone_old_skill_cannot_replace_upgraded_profile(tmp_path, monkeypatch):
    remote = {}
    monkeypatch.setattr(SSHEnvironment, '_establish_connection', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, '_detect_remote_home', lambda self: '/workbench')
    monkeypatch.setattr(SSHEnvironment, '_ensure_remote_dirs', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, 'init_session', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, 'cleanup', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, '_ssh_bulk_upload',
                        lambda self, files: remote.update({dst: Path(src).read_bytes() for src, dst in files}))
    monkeypatch.setattr(SSHEnvironment, '_ssh_delete', lambda self, paths: [remote.pop(p, None) for p in paths])

    def download(self, dest):
        with tarfile.open(dest, 'w') as archive:
            for path, content in remote.items():
                info = tarfile.TarInfo(path.lstrip('/'))
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    monkeypatch.setattr(SSHEnvironment, '_ssh_bulk_download', download)
    a, b = tmp_path / 'A', tmp_path / 'B'
    skill = Path('skills/report/SKILL.md')
    (a / skill).parent.mkdir(parents=True)
    (a / skill).write_text('---\nname: report\ndescription: report\n---\nV1')
    shutil.copytree(a, b)
    with profile(a):
        env_a = SSHEnvironment('workbench', 'user')
        (a / skill).write_text('---\nname: report\ndescription: report\n---\nV2 upgraded')
        env_a._sync_manager.sync(force=True)
    with profile(b):
        SSHEnvironment('workbench', 'user')
    # Deferred cleanup runs with a different profile still selected.
    with profile(b):
        env_a._sync_manager.sync_back(hermes_home=a)
    assert (a / skill).read_text().endswith('V2 upgraded')
    assert (b / skill).read_text().endswith('V1')

    # Fresh environments (including after a workbench reset) seed from host state.
    remote.clear()
    with profile(a):
        restarted = SSHEnvironment('workbench', 'user')
        assert any(content.endswith(b'V2 upgraded') for content in remote.values())
        restarted._sync_manager.sync_back(hermes_home=a)
    assert (a / skill).read_text().endswith('V2 upgraded')

    # Terminal edits are execution copies, never an installation/update to host skills.
    for path in list(remote):
        if path.endswith('/SKILL.md'):
            remote[path] = b'V1 stale terminal edit'
    restarted._sync_manager.sync_back(hermes_home=a)
    assert (a / skill).read_text().endswith('V2 upgraded')
    (a / skill).unlink()
    restarted._sync_manager.sync_back(hermes_home=a)
    assert not (a / skill).exists()


def test_ssh_scripts_and_images_resolve_the_synced_profile(tmp_path, monkeypatch):
    import json
    import shlex
    import subprocess
    import sys
    from tools import image_generation_tool
    from tools.environments import ssh as ssh_module

    monkeypatch.setattr(SSHEnvironment, '_establish_connection', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, '_detect_remote_home', lambda self: str(tmp_path / 'remote'))
    monkeypatch.setattr(SSHEnvironment, 'init_session', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, 'cleanup', lambda self: None)
    with profile(tmp_path / 'host'):
        env = SSHEnvironment('workbench', 'user', sync_files=False)
        root = Path(env.agent_visible_cache_base())
        root.mkdir(parents=True)
        (root / 'google_token.json').write_text('profile-token-fixture')
        # Run the command payload with a real shell; only the SSH transport is substituted.
        def local_bash(argv, stdin_data=None, **kwargs):
            return subprocess.Popen(['bash', '-c', shlex.split(argv[-1])[0]],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        monkeypatch.setattr(ssh_module, '_popen_bash', local_bash)
        script = Path(__file__).resolve().parents[2] / 'skills/productivity/google-workspace/scripts/google_api.py'
        code = f"import runpy; m=runpy.run_path({str(script)!r}); print(m['TOKEN_PATH'].read_text())"
        proc = env._run_bash(shlex.join([sys.executable, '-c', code]))
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, stderr
        assert stdout.strip() == b'profile-token-fixture'
        assert image_generation_tool._agent_cache_base_for_env(env) == str(root)
        monkeypatch.setenv('TERMINAL_ENV', 'ssh')
        assert image_generation_tool._agent_cache_base_for_env(None).replace('~', str(tmp_path / 'remote'), 1) == str(root)
        (root / 'google_token.json').unlink()
