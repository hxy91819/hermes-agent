"""A runtime snapshot without build scripts still supports the image installer."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.platforms('linux')
def test_package_restores_image_installer_and_survives_relocation(tmp_path):
    spec = importlib.util.spec_from_file_location('fork_package', ROOT / 'scripts/fork-package.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / 'source'
    (source / '.fork').mkdir(parents=True)
    (source / '.fork/branches').write_text('base v0.22.1\n', encoding='utf-8')
    (source / 'scripts').mkdir()
    for name in ('install.sh', 'fork-image-install.sh'):
        shutil.copy2(ROOT / 'scripts' / name, source / 'scripts' / name)
    for args in (['init'], ['add', '.'], ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'fixture']):
        subprocess.run(['git', *args], cwd=source, check=True, capture_output=True, timeout=30)
    payload = tmp_path / 'payload'
    (payload / 'hermes-agent').mkdir(parents=True)
    (payload / 'hermes-agent/pyproject.toml').write_text('[project]\nname="hermes-agent"\nversion = "0.0.0"\n', encoding='utf-8')
    (payload / 'bin').mkdir()
    command = payload / 'bin/hermes'
    command.write_text('#!/bin/sh\necho "Hermes Agent v0.22.1"\n', encoding='utf-8')
    command.chmod(0o755)
    (payload / 'venv/bin').mkdir(parents=True)
    out = tmp_path / 'dist'
    module.package(payload, out, 'fork-v0.22.1-test', source)
    moved = tmp_path / 'relocated'
    moved.mkdir()
    with tarfile.open(out / 'hermes-agent.tar.gz') as archive:
        archive.extractall(moved, filter='data')
    installed = moved / 'hermes-agent'
    home = tmp_path / 'clean-home'
    home.mkdir()
    env = {'HOME': str(home), 'PATH': os.environ['PATH']}
    for stage in ('venv', 'python-deps', 'node-deps', 'path', 'config'):
        subprocess.run(['bash', str(installed / 'scripts/install.sh'), '--stage', stage], env=env, check=True, capture_output=True, timeout=30)
    result = subprocess.run([str(installed / 'venv/bin/hermes'), '--version'], env=env, check=True, capture_output=True, text=True, timeout=30)
    assert result.stdout.strip() == 'Hermes Agent v0.22.1'
    assert (home / '.hermes/cron').is_dir()
    assert (home / '.local/bin/hermes').is_file()
    metadata = json.loads((out / 'latest.json').read_text(encoding='utf-8-sig'))
    assert metadata['sha256'] == module.file_sha256(out / 'hermes-agent.tar.gz')
    assert metadata['src_version'] == '0.22.1'
