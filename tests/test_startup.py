import os
import plistlib
import subprocess
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


def test_launchagent_host_configuration_persists_and_reloads(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / 'scripts/start.sh'
    source = script.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setenv('MEDIALINK_PROJECT', str(script.parent.parent))
    monkeypatch.setenv('MEDIALINK_ALLOWED_HOSTS', 'demo.example.org')
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, 'run', run)
    response = MagicMock()
    response.__enter__.return_value.status = 200
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *args, **kwargs: response)
    plist = tmp_path / 'Library/LaunchAgents/asia.viol1n.medialink.plist'
    exec(compile(source, str(script), 'exec'), {})
    assert plistlib.loads(plist.read_bytes())['EnvironmentVariables']['MEDIALINK_ALLOWED_HOSTS'] == 'demo.example.org'
    assert [command[1] for command in commands] == ['print', 'bootout', 'bootstrap']

    monkeypatch.delenv('MEDIALINK_ALLOWED_HOSTS')
    commands.clear()
    exec(compile(source, str(script), 'exec'), {})
    assert plistlib.loads(plist.read_bytes())['EnvironmentVariables']['MEDIALINK_ALLOWED_HOSTS'] == 'demo.example.org'
    assert [command[1] for command in commands] == ['print', 'kickstart']

    monkeypatch.setenv('MEDIALINK_ALLOWED_HOSTS', 'media.example.com')
    commands.clear()
    exec(compile(source, str(script), 'exec'), {})
    assert plistlib.loads(plist.read_bytes())['EnvironmentVariables']['MEDIALINK_ALLOWED_HOSTS'] == os.environ['MEDIALINK_ALLOWED_HOSTS']
    assert [command[1] for command in commands] == ['print', 'bootout', 'bootstrap']
