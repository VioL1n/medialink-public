import pytest
from fastapi.testclient import TestClient
from medialink.access import host_allowed, migrate_public_access, normalize_public_hosts
from medialink.app import create_app
from medialink.core import DEFAULTS, Problem, Store
from test_medialink import MemoryVault


@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost', '192.168.5.168', '::1', 'fd00::1'])
def test_local_access_with_public_access_disabled(host):
    assert host_allowed(host, DEFAULTS)


@pytest.mark.parametrize('host', ['media.example.com', '8.8.8.8', '2001:4860:4860::8888'])
def test_public_domains_and_ips_require_both_switch_and_allowlist(host):
    settings = {**DEFAULTS, 'public_access_hosts': host}
    assert not host_allowed(host, settings)
    settings['public_access_enabled'] = True
    assert host_allowed(host, settings)
    assert not host_allowed('evil.example', settings)


def test_host_list_normalization():
    assert normalize_public_hosts(' MEDIA.EXAMPLE.COM.\nmedia.example.com, 8.8.8.8, [2001:4860:4860::8888] ') == (
        'media.example.com,8.8.8.8,2001:4860:4860::8888')
    assert normalize_public_hosts('例子.com') == 'xn--fsqu00a.com'


@pytest.mark.parametrize('hosts', ['https://media.example.com', 'media.example.com:8766', 'media.example.com/path', '*',
                                 '-invalid.example', 'a' * 2050, ['media.example.com']])
def test_invalid_host_list_rejected(hosts):
    with pytest.raises(Problem):
        normalize_public_hosts(hosts)


def test_legacy_hosts_import_once_and_do_not_override_disabled_switch(tmp_path):
    store = Store(tmp_path / 'state.db')
    migrate_public_access(store, ' MEDIA.EXAMPLE.COM ')
    assert store.settings()['public_access_enabled'] is True
    assert store.settings()['public_access_hosts'] == 'media.example.com'
    store.configure({'public_access_enabled': False})
    migrate_public_access(store, 'other.example')
    assert store.settings()['public_access_enabled'] is False
    assert store.settings()['public_access_hosts'] == 'media.example.com'


def test_settings_toggle_takes_effect_and_survives_app_restart(tmp_path, monkeypatch):
    monkeypatch.delenv('MEDIALINK_ALLOWED_HOSTS', raising=False)
    store, vault = Store(tmp_path / 'state.db'), MemoryVault()
    app = create_app(store, vault, start_workers=False)
    with TestClient(app) as client:
        assert client.patch('/api/v1/settings', json={'public_access_enabled': True}).status_code == 401
        client.post('/api/v1/auth/setup', json={'password': 'test-password-123'})
        assert client.get('/api/v1/settings').json()['public_access_enabled'] is False
        headers = {'Host': 'media.example.com:8766'}
        assert client.get('/api/v1/health', headers=headers).status_code == 403
        for invalid in ['true', 1, None]:
            assert client.patch('/api/v1/settings', json={'public_access_enabled': invalid}).status_code == 422
        assert client.patch('/api/v1/settings', json={'public_access_enabled': True}).status_code == 422
        assert client.patch('/api/v1/settings', json={'public_access_hosts': '*'}).status_code == 422
        saved = client.patch('/api/v1/settings', json={'public_access_enabled': True,
                                                     'public_access_hosts': ' MEDIA.EXAMPLE.COM,8.8.8.8 '})
        assert saved.status_code == 200
        assert saved.json()['public_access_hosts'] == 'media.example.com,8.8.8.8'
        assert client.get('/api/v1/health', headers=headers).status_code == 200
        assert client.get('/api/v1/health', headers={'Host': '8.8.8.8:8766'}).status_code == 200
        assert client.get('http://media.example.com:8766/api/v1/tasks').status_code == 401
        assert client.patch('/api/v1/settings', json={'public_access_enabled': False}).status_code == 200
        assert client.get('/api/v1/health', headers=headers).status_code == 403
        assert client.get('/api/v1/health').status_code == 200
    monkeypatch.setenv('MEDIALINK_ALLOWED_HOSTS', 'media.example.com')
    with TestClient(create_app(store, vault, start_workers=False)) as restarted:
        assert restarted.get('/api/v1/health', headers=headers).status_code == 403
        assert store.settings()['public_access_enabled'] is False


def test_public_login_keeps_origin_and_local_setup_checks(tmp_path, monkeypatch):
    monkeypatch.delenv('MEDIALINK_ALLOWED_HOSTS', raising=False)
    store = Store(tmp_path / 'state.db')
    store.configure({'public_access_enabled': True, 'public_access_hosts': 'media.example.com'})
    app = create_app(store, MemoryVault(), start_workers=False)
    with TestClient(app, base_url='http://media.example.com:8766', client=('8.8.4.4', 12345)) as remote:
        body = {'password': 'test-password-123'}
        assert remote.post('/api/v1/auth/setup', json=body).status_code == 403
        assert remote.post('/api/v1/auth/login', headers={'Origin': 'http://evil.example'}, json=body).status_code == 403
