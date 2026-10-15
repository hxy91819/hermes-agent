"""Profiles sharing a sandbox choose separate desktop state and X displays."""
from types import SimpleNamespace

import pytest

from tools.bot_desktop import sandbox_host


def test_profiles_have_separate_runtime_directories_and_displays(monkeypatch):
    env = SimpleNamespace(get_temp_dir=lambda: '/tmp')
    assert sandbox_host._remote_dir(env, 'alpha') != sandbox_host._remote_dir(env, 'beta')
    config = {'bot_desktop': {'display_number': 21}}
    monkeypatch.setattr('hermes_cli.config.load_config', lambda: config)
    assert sandbox_host._display_number() == 21
    config.clear()
    assert sandbox_host._display_number() == 20


@pytest.mark.parametrize('value', [False, 0, '21', -1, 65536])
def test_invalid_display_number_is_reported(monkeypatch, value):
    monkeypatch.setattr('hermes_cli.config.load_config', lambda: {'bot_desktop': {'display_number': value}})
    with pytest.raises(ValueError, match='display_number'):
        sandbox_host._display_number()
