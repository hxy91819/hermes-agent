"""Shared SSH workbenches must not roll one profile's skills back from another."""
import io
import json
import shutil
import tarfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.environments.ssh import SSHEnvironment


@contextmanager
def profile(home):
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("callback_profile", ["A", "B"])
def test_clone_old_skill_cannot_replace_upgraded_profile(tmp_path, monkeypatch, callback_profile):
    from tools.skill_manager_tool import skill_manage

    monkeypatch.setenv('TERMINAL_ENV', 'ssh')
    remote = {}
    uploads = {}
    monkeypatch.setattr(SSHEnvironment, '_establish_connection', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, '_detect_remote_home', lambda self: '/workbench')
    monkeypatch.setattr(SSHEnvironment, '_ensure_remote_dirs', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, 'init_session', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, 'cleanup', lambda self: None)
    def upload(self, files):
        uploads.setdefault(self, {}).update({dst: Path(src).read_bytes() for src, dst in files})
        remote.update({dst: Path(src).read_bytes() for src, dst in files})
    monkeypatch.setattr(SSHEnvironment, '_ssh_bulk_upload', upload)
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
    cache = Path('cache/images/result.txt')
    (a / cache).parent.mkdir(parents=True)
    (a / cache).write_text('original cache')
    shutil.copytree(a, b)
    with profile(a):
        env_a = SSHEnvironment('workbench', 'user')
        result = json.loads(skill_manage(
            action='edit', name='report',
            content='---\nname: report\ndescription: report\n---\nV2 upgraded'))
        assert result['success'] is True
        assert (a / skill).read_text().endswith('V2 upgraded')
        assert (b / skill).read_text().endswith('V1')
        env_a._sync_manager.sync(force=True)
    with profile(b):
        env_b = SSHEnvironment('workbench', 'user')
    a_skill_remote = next(dst for dst in uploads[env_a] if dst.endswith('/skills/report/SKILL.md'))
    b_skill_remote = next(dst for dst in uploads[env_b] if dst.endswith('/skills/report/SKILL.md'))
    a_cache_remote = next(dst for dst in uploads[env_a] if dst.endswith('/cache/images/result.txt'))
    b_cache_remote = next(dst for dst in uploads[env_b] if dst.endswith('/cache/images/result.txt'))
    remote[a_cache_remote] = b'A remote result'
    # Cover both ordinary A cleanup and A's delayed callback after selecting B.
    with profile(a if callback_profile == 'A' else b):
        env_a._sync_manager.sync_back(hermes_home=a)
    assert (a / skill).read_text().endswith('V2 upgraded')
    assert (b / skill).read_text().endswith('V1')
    assert a_skill_remote != b_skill_remote
    assert remote[a_skill_remote].endswith(b'V2 upgraded')
    assert remote[b_skill_remote].endswith(b'V1')
    assert a_cache_remote != b_cache_remote
    assert (a / cache).read_text() == 'A remote result'
    assert (b / cache).read_text() == 'original cache'
    # Delayed uploads must also enumerate A's files, not the active B profile.
    (a / skill).write_text((a / skill).read_text().replace('V2 upgraded', 'A delayed edit: V2 upgraded'))
    with profile(b):
        env_a._sync_manager.sync(force=True)
    assert uploads[env_a][a_skill_remote].endswith(b'A delayed edit: V2 upgraded')
    assert b_skill_remote not in uploads[env_a]
    assert remote[a_skill_remote].endswith(b'V2 upgraded')

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
    with profile(a):
        restarted._sync_manager.sync_back(hermes_home=a)
        assert (a / skill).read_text().endswith('V2 upgraded')
        (a / skill).unlink()
        restarted._sync_manager.sync_back(hermes_home=a)
    assert not (a / skill).exists()


def test_ssh_scripts_and_images_resolve_the_synced_profile(tmp_path, monkeypatch):
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


def test_new_ssh_environment_removes_uninstalled_skill_copies(tmp_path, monkeypatch):
    """An uninstall run outside this process (host CLI/ACLI) leaves no executable copy.

    Deletion of remote skill files used to depend on the in-process ``_synced_files``
    map, so a fresh environment (new session or gateway restart) enumerated only the
    current host files and never noticed the copy an earlier environment uploaded.
    """
    monkeypatch.setenv('TERMINAL_ENV', 'ssh')
    remote = {}
    uploads = {}
    monkeypatch.setattr(SSHEnvironment, '_establish_connection', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, '_detect_remote_home', lambda self: '/workbench')
    monkeypatch.setattr(SSHEnvironment, '_ensure_remote_dirs', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, 'init_session', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, 'cleanup', lambda self: None)

    def upload(self, files):
        uploads.setdefault(self, {}).update({dst: Path(src).read_bytes() for src, dst in files})
        remote.update({dst: Path(src).read_bytes() for src, dst in files})
    monkeypatch.setattr(SSHEnvironment, '_ssh_bulk_upload', upload)

    def delete(self, paths):
        for path in paths:
            remote.pop(path, None)
    monkeypatch.setattr(SSHEnvironment, '_ssh_delete', delete)
    # Seams the reconciliation uses; raising=False keeps this a behavioural red test
    # against the unfixed backend instead of failing on a missing attribute.
    def list_remote(self):
        roots = self._skill_sync_roots()
        return {path for path in remote
                if any(path == root or path.startswith(root + '/') for root in roots)}
    monkeypatch.setattr(SSHEnvironment, '_list_remote_skill_files', list_remote, raising=False)
    def prune(self, dirs):
        for directory in sorted(dirs, key=len, reverse=True):
            for path in list(remote):
                if path.startswith(directory + '/'):
                    remote.pop(path, None)
    monkeypatch.setattr(SSHEnvironment, '_prune_empty_remote_dirs', prune, raising=False)

    def write(home, relative, text):
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    homes = {'A': tmp_path / 'A', 'B': tmp_path / 'B'}
    for label, home in homes.items():
        for name, body in (('removed', 'V2' if label == 'A' else 'V1'),
                           ('live', 'V2' if label == 'A' else 'V1')):
            write(home, f'skills/testing/{name}/SKILL.md',
                  f'---\nname: {name}\ndescription: {name}\n---\n{body}')
            write(home, f'skills/testing/{name}/scripts/report.py', f'print("{body}")')
        write(home, 'cache/images/result.txt', f'{label} cache')

    envs = {}
    for label, home in homes.items():
        with profile(home):
            envs[label] = SSHEnvironment('workbench', 'user')

    def remote_dir(label, skill):
        return next(dst for dst in uploads[envs[label]] if dst.endswith(f'/skills/testing/{skill}/SKILL.md')).rsplit('/', 1)[0]

    a_removed = remote_dir('A', 'removed')
    a_live = remote_dir('A', 'live')
    b_removed = remote_dir('B', 'removed')
    b_live = remote_dir('B', 'live')
    # Runtime-only artifacts: a removed skill's whole directory must go, but files the
    # agent generated inside a still-installed skill (venv, node_modules, ...) stay.
    remote[a_removed + '/.venv/bin/python'] = b'stale runtime'
    remote[a_live + '/.venv/bin/python'] = b'live runtime'
    remote[b_live + '/.venv/bin/python'] = b'other profile runtime'

    shutil.rmtree(homes['A'] / 'skills/testing/removed')

    with profile(homes['A']):
        SSHEnvironment('workbench', 'user')
    assert not any(path.startswith(a_removed + '/') for path in remote)
    assert remote[a_live + '/.venv/bin/python'] == b'live runtime'
    assert remote[a_live + '/SKILL.md'].endswith(b'V2')
    assert remote[a_live + '/scripts/report.py'] == b'print("V2")'
    assert remote[a_live.rsplit('/skills/', 1)[0] + '/cache/images/result.txt'] == b'A cache'
    # Other profiles and non-skill cache entries are untouched.
    assert any(path.startswith(b_removed + '/') for path in remote)
    assert remote[b_live + '/.venv/bin/python'] == b'other profile runtime'
    assert remote[b_live.rsplit('/skills/', 1)[0] + '/cache/images/result.txt'] == b'B cache'

    # A later restart must not re-upload the removed skill from host state.
    with profile(homes['A']):
        SSHEnvironment('workbench', 'user')
    assert not any(path.startswith(a_removed + '/') for path in remote)
    assert not (homes['A'] / 'skills/testing/removed').exists()


def test_remote_skill_listing_drops_paths_outside_profile_roots(tmp_path, monkeypatch):
    """A hostile/buggy remote listing cannot widen rm beyond this profile's roots."""
    monkeypatch.setenv('TERMINAL_ENV', 'ssh')
    monkeypatch.setattr(SSHEnvironment, '_establish_connection', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, '_detect_remote_home', lambda self: '/workbench')
    monkeypatch.setattr(SSHEnvironment, 'init_session', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, 'cleanup', lambda self: None)
    with profile(tmp_path / 'host'):
        env = SSHEnvironment('workbench', 'user', sync_files=False)
        root = env._skill_sync_roots()[0]
        inside = f'{root}/testing/live/SKILL.md'
        stdout = '\0'.join([
            inside,
            f'{root}/../../etc/passwd',
            '/root/.hermes/skills/legacy/SKILL.md',
            f'{root}/testing/live/../live/scripts/report.py',
        ]) + '\0'
        monkeypatch.setattr(SSHEnvironment, '_run_ssh', lambda self, command, timeout: type(
            'R', (), {'returncode': 0, 'stdout': stdout, 'stderr': ''})())
        assert env._list_remote_skill_files() == {
            inside, f'{root}/testing/live/scripts/report.py'}


def test_skill_reconcile_never_widens_to_a_sync_root_or_live_nested_skill(tmp_path, monkeypatch):
    """A stale SKILL.md at a root, or above a live nested skill, must not widen removal."""
    monkeypatch.setattr(SSHEnvironment, '_establish_connection', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, '_detect_remote_home', lambda self: '/workbench')
    monkeypatch.setattr(SSHEnvironment, 'init_session', lambda self: None)
    monkeypatch.setattr(SSHEnvironment, 'cleanup', lambda self: None)
    with profile(tmp_path / 'host'):
        env = SSHEnvironment('workbench', 'user', sync_files=False)
        root = env._skill_sync_roots()[0]
        parent = f'{root}/testing/parent'
        child = f'{parent}/child'
        remote = {
            f'{root}/SKILL.md',
            f'{parent}/SKILL.md',
            f'{parent}/old.py',
            f'{child}/SKILL.md',
            f'{child}/run.py',
        }
        expected = {f'{child}/SKILL.md', f'{child}/run.py'}
        stale_files, stale_dirs = env._stale_remote_skill_files(remote, expected)
        assert stale_dirs == {parent}
        assert set(stale_files) == {f'{parent}/SKILL.md', f'{parent}/old.py'}
