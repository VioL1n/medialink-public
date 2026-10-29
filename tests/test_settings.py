import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from medialink.app import create_app, password_hash
from medialink.core import SEARCH_SOURCE_IDS, Problem, Store
from test_medialink import MemoryVault, authenticate

OLD_PASSWORD = "test-password-123"
NEW_PASSWORD = "new-password-456"


@pytest.fixture
def settings_client(tmp_path):
    store, vault = Store(tmp_path / "state.db"), MemoryVault()
    app = create_app(store, vault, start_workers=False)
    with TestClient(app) as client:
        authenticate(client)
        yield client, store, vault


def test_search_preferences_defaults_catalog_and_restart(settings_client):
    client, store, vault = settings_client
    settings = client.get('/api/v1/settings').json()
    assert settings['enabled_search_sources'] == list(SEARCH_SOURCE_IDS)
    assert settings['default_use_tmdb'] is True and settings['magnet_use_proxy'] is False
    saved = client.patch('/api/v1/settings', json={
        'enabled_search_sources': ['nyaa', 'telegram', 'nyaa'], 'default_use_tmdb': False, 'magnet_use_proxy': True,
    })
    assert saved.status_code == 200
    assert saved.json()['enabled_search_sources'] == ['nyaa', 'telegram']
    assert [s['id'] for s in client.get('/api/v1/sources').json()] == list(SEARCH_SOURCE_IDS)
    assert client.patch('/api/v1/settings', json={'proxy': 'socks5://127.0.0.1:7890'}).status_code == 200
    with TestClient(create_app(Store(store.path), vault, start_workers=False)) as restarted:
        assert restarted.post('/api/v1/auth/login', json={'password': OLD_PASSWORD}).status_code == 200
        settings = restarted.get('/api/v1/settings').json()
        assert settings['enabled_search_sources'] == ['nyaa', 'telegram']
        assert settings['default_use_tmdb'] is False and settings['magnet_use_proxy'] is True
        assert store.channels() == []


@pytest.mark.parametrize('values', [
    {'enabled_search_sources': 'nyaa'}, {'enabled_search_sources': None},
    {'enabled_search_sources': ['unknown']}, {'enabled_search_sources': [1]},
    {'enabled_search_sources': [[]]}, {'enabled_search_sources': {'nyaa': True}},
    {'default_use_tmdb': 'false'}, {'default_use_tmdb': 0}, {'default_use_tmdb': None},
    {'magnet_use_proxy': 'false'}, {'magnet_use_proxy': 1}, {'magnet_use_proxy': None},
])
def test_invalid_preferences_do_not_change_saved_settings(settings_client, values):
    client, store, _ = settings_client
    previous = store.settings()
    assert client.patch('/api/v1/settings', json=values).status_code == 422
    assert store.settings() == previous


def test_search_uses_saved_sources_and_tmdb_default_with_explicit_override(settings_client):
    client, _, _ = settings_client
    engine = client.app.state.search_engine
    called = []

    async def execute(data, job):
        called.append(data)
        job['done'] = True

    engine.execute = execute
    client.patch('/api/v1/settings', json={'enabled_search_sources': ['nyaa', 'telegram'], 'default_use_tmdb': False})
    for extra in ({}, {'sources': ['telegram'], 'use_tmdb': True}, {'sources': None, 'use_tmdb': None}):
        result = client.post('/api/v1/search', json={'query': 'Movie', **extra})
        assert result.status_code == 200
        client.get('/api/v1/search/'+result.json()['id']+'/events')
    assert [(r.sources, r.use_tmdb) for r in called] == [
        (['nyaa', 'telegram'], False), (['telegram'], True), (['nyaa', 'telegram'], False),
    ]
    for selected in ([], ['knaben'], ['unknown'], ['nyaa', 'knaben']):
        assert client.post('/api/v1/search', json={'query': 'Movie', 'sources': selected}).status_code == 422
    assert len(called) == 3


@pytest.mark.parametrize('media', [None, {'type': 'movie', 'id': 100}, {'type': 'tv', 'id': 100, 'season_number': 2}])
def test_only_enabled_sources_are_called_for_regular_and_media_search(settings_client, media):
    client, _, _ = settings_client
    app = client.app
    client.patch('/api/v1/settings', json={'enabled_search_sources': ['nyaa'], 'default_use_tmdb': False})
    app.state.nyaa.search = AsyncMock(return_value=[])
    app.state.knaben.search = AsyncMock(return_value=[])
    app.state.telegram.search = AsyncMock(return_value=[])
    app.state.tmdb.state = AsyncMock(return_value={'tmdb_configured': True})
    app.state.tmdb.search = AsyncMock(return_value={'items': []})
    app.state.tmdb.detail = AsyncMock(return_value={
        'type': media['type'] if media else 'movie', 'id': 100, 'title': 'Movie',
        'english_title': 'Movie', 'year': 2020, 'seasons': [{'number': 2}], 'updated': 1, 'stale': False,
    })
    result = client.post('/api/v1/search', json={'query': 'Movie', 'media': media})
    assert result.status_code == 200
    events = client.get('/api/v1/search/'+result.json()['id']+'/events').text
    assert 'event: done' in events and '"provider": "nyaa"' in events
    app.state.nyaa.search.assert_awaited()
    app.state.knaben.search.assert_not_awaited()
    app.state.telegram.search.assert_not_awaited()
    if media:
        app.state.tmdb.detail.assert_awaited_once_with(media['type'], 100)
        assert '"status": "resolved"' in events
    else:
        app.state.tmdb.detail.assert_not_awaited()
        app.state.tmdb.search.assert_not_awaited()
        assert '"status": "disabled"' in events


def test_disable_all_preserves_channels_credentials_and_allows_manual_ingestion(settings_client):
    client, store, vault = settings_client
    with store.connect() as db:
        db.execute("INSERT INTO channels(id,peer,title) VALUES ('1','example','Example')")
    vault.set('telegram-session', 'saved-session')
    client.app.state.search_engine.execute = AsyncMock()
    assert client.patch('/api/v1/settings', json={'enabled_search_sources': []}).status_code == 200
    assert client.get('/api/v1/settings').json()['enabled_search_sources'] == []
    assert client.post('/api/v1/search', json={'query': 'Movie'}).status_code == 422
    client.app.state.search_engine.execute.assert_not_awaited()
    assert store.channels()[0]['enabled'] == 1 and vault.get('telegram-session') == 'saved-session'
    client.app.state.openlist.get = AsyncMock(return_value={'is_dir': True})
    result = client.post('/api/v1/tasks', json={
        'link': 'magnet:?xt=urn:btih:'+'a'*40, 'title': 'Manual', 'target': '/115/infuse',
    })
    assert result.status_code == 200


def test_password_change_revokes_all_devices_and_survives_restart(settings_client):
    client, store, vault = settings_client
    old_session = client.cookies.get('medialink_session')
    other_session = store.new_session()
    result = client.post('/api/v1/auth/password', json={'current_password': OLD_PASSWORD, 'new_password': NEW_PASSWORD})
    assert result.status_code == 200 and result.json() == {'ok': True}
    assert 'Max-Age=0' in result.headers['set-cookie']
    assert not store.session_valid(old_session) and not store.session_valid(other_session)
    assert client.get('/api/v1/settings').status_code == 401
    assert client.post('/api/v1/auth/login', json={'password': OLD_PASSWORD}).status_code == 401
    assert client.post('/api/v1/auth/login', json={'password': NEW_PASSWORD}).status_code == 200
    record = json.loads(vault.get('password'))
    assert record['hash'] == password_hash(NEW_PASSWORD, record['salt'])
    with store.connect() as db:
        assert not any('password' in row['key'] for row in db.execute('SELECT * FROM settings'))
    with TestClient(create_app(Store(store.path), vault, start_workers=False)) as restarted:
        assert restarted.post('/api/v1/auth/login', json={'password': NEW_PASSWORD}).status_code == 200


def test_password_wrong_current_and_failed_vault_write_preserve_login(settings_client, monkeypatch):
    client, store, vault = settings_client
    token = client.cookies.get('medialink_session')
    previous = vault.get('password')
    body = {'current_password': 'wrong-password', 'new_password': NEW_PASSWORD}
    assert client.post('/api/v1/auth/password', json=body).status_code == 403
    assert store.session_valid(token)
    body['current_password'] = OLD_PASSWORD

    def fail_write(*args):
        raise Problem('无法保存到 macOS Keychain', 503)

    monkeypatch.setattr(vault, 'set', fail_write)
    result = client.post('/api/v1/auth/password', json=body)
    assert result.status_code == 503 and OLD_PASSWORD not in result.text and NEW_PASSWORD not in result.text
    assert vault.get('password') == previous and store.session_valid(token)


@pytest.mark.parametrize('new_password', ['short', 'x'*201, OLD_PASSWORD, None, ['secret']])
def test_invalid_new_password_is_rejected_without_reflecting_secrets(settings_client, new_password):
    client, store, vault = settings_client
    previous = vault.get('password')
    result = client.post('/api/v1/auth/password', json={'current_password': OLD_PASSWORD, 'new_password': new_password})
    assert result.status_code == 422 and OLD_PASSWORD not in result.text
    assert vault.get('password') == previous and store.session_valid(client.cookies.get('medialink_session'))


def test_password_endpoint_auth_origin_and_shared_rate_limit(settings_client):
    client, _, _ = settings_client
    body = {'current_password': 'wrong-password', 'new_password': NEW_PASSWORD}
    assert client.post('/api/v1/auth/password', headers={'Origin': 'https://evil.example'}, json=body).status_code == 403
    for _ in range(5):
        assert client.post('/api/v1/auth/password', json=body).status_code == 403
    assert client.post('/api/v1/auth/password', json=body).status_code == 429
    assert client.post('/api/v1/auth/login', json={'password': OLD_PASSWORD}).status_code == 429
    client.post('/api/v1/auth/logout')
    assert client.post('/api/v1/auth/password', json=body).status_code == 401


def test_sse_open_stream_stops_when_its_session_is_revoked(settings_client):
    client, store, _ = settings_client
    entered = threading.Event()

    async def execute(data, job):
        entered.set()
        await asyncio.sleep(0.05)
        store.revoke_sessions()
        job['events'].append({'provider': 'nyaa', 'results': [], 'status': 'done'})
        job['done'] = True

    client.app.state.search_engine.execute = execute
    result = client.post('/api/v1/search', json={'query': 'Movie', 'sources': ['nyaa']})
    assert entered.wait(1)
    events = client.get('/api/v1/search/'+result.json()['id']+'/events').text
    assert 'event: auth_required' in events and 'event: done' not in events
    assert '"provider"' not in events


@pytest.mark.parametrize('login_first', [True, False])
def test_concurrent_old_password_login_cannot_leave_valid_session(settings_client, monkeypatch, login_first):
    client, store, vault = settings_client
    entered, release = threading.Event(), threading.Event()
    original_get, original_set = vault.get, vault.set

    def pause_get(name, *args):
        if name == 'password' and not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original_get(name, *args)

    def pause_set(name, value):
        if name == 'password':
            entered.set()
            assert release.wait(3)
        return original_set(name, value)

    monkeypatch.setattr(vault, 'get' if login_first else 'set', pause_get if login_first else pause_set)
    headers = {'Cookie': 'medialink_session='+client.cookies.get('medialink_session')}
    with ThreadPoolExecutor(max_workers=2) as pool:
        def login():
            return client.post('/api/v1/auth/login', json={'password': OLD_PASSWORD})

        def change():
            return client.post('/api/v1/auth/password', headers=headers, json={
                'current_password': OLD_PASSWORD, 'new_password': NEW_PASSWORD,
            })

        first = pool.submit(login if login_first else change)
        assert entered.wait(3)
        second = pool.submit(change if login_first else login)
        release.set()
        results = [first.result(5), second.result(5)]
    assert results[0 if not login_first else 1].status_code == 200
    login_result = results[0 if login_first else 1]
    if login_first:
        assert login_result.status_code == 200
        assert not store.session_valid(login_result.cookies.get('medialink_session'))
    else:
        assert login_result.status_code == 401
