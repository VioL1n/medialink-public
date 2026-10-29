import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from medialink.app import create_app, password_hash
from medialink.cli import initialize_password
from medialink.clients import readonly_database
from medialink.core import DEFAULTS, SERVICE, Problem, Store, Vault
from medialink.file_vault import FileVault


def file_vault(monkeypatch, tmp_path):
    monkeypatch.setenv('MEDIALINK_VAULT_BACKEND', 'file')
    monkeypatch.setenv('MEDIALINK_VAULT_DIR', str(tmp_path/'vault'))
    return Vault()


def test_file_vault_restart_encryption_and_permissions(monkeypatch, tmp_path):
    vault = file_vault(monkeypatch, tmp_path)
    secret = 'FAKE-COOKIE-FOR-ISOLATED-TEST'
    vault.set('115-cookies', secret)
    encrypted_link = vault.seal('https://115.com/s/test-only')
    restarted = Vault()
    assert restarted.get('115-cookies') == secret
    assert restarted.get('115-cookies', 'other-service') is None
    assert restarted.unseal(encrypted_link) == 'https://115.com/s/test-only'
    directory = tmp_path/'vault'
    assert secret.encode() not in (directory/'credentials.enc').read_bytes()
    assert directory.stat().st_mode & 0o777 == 0o700
    for name in ('master.key', 'credentials.enc', '.lock'):
        assert (directory/name).stat().st_mode & 0o777 == 0o600
    restarted.delete('115-cookies')
    assert Vault().get('115-cookies') is None
    assert Vault().unseal(encrypted_link) == 'https://115.com/s/test-only'


def test_missing_key_or_corrupt_vault_never_silently_resets(monkeypatch, tmp_path):
    vault = file_vault(monkeypatch, tmp_path)
    vault.set('test', 'test-value')
    key = tmp_path/'vault/master.key'
    original = key.read_bytes()
    key.unlink()
    with pytest.raises(Problem, match='主密钥丢失'):
        vault.set('new', 'value')
    assert not key.exists()
    key.write_bytes(original)
    ciphertext = tmp_path/'vault/credentials.enc'
    ciphertext.write_bytes(b'corrupt-test-data')
    with pytest.raises(Problem, match='无法解密'):
        vault.set('new', 'value')
    assert ciphertext.read_bytes() == b'corrupt-test-data'


def test_vault_concurrent_writes_preserve_all_fields(tmp_path):
    def write(index):
        FileVault(tmp_path/'vault').set(SERVICE, str(index), 'value-'+str(index))
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(24)))
    vault = FileVault(tmp_path/'vault')
    for index in range(24):
        assert vault.get(SERVICE, str(index)) == 'value-'+str(index)


def test_missing_ciphertext_and_symlink_fail_closed(monkeypatch, tmp_path):
    vault = file_vault(monkeypatch, tmp_path)
    vault.set('test', 'fake-secret')
    path = tmp_path/'vault/credentials.enc'
    path.unlink()
    with pytest.raises(Problem, match='凭据文件丢失'):
        vault.get('test')
    outside = tmp_path/'unrelated'
    outside.write_text('do not read or change')
    path.symlink_to(outside)
    with pytest.raises(Problem, match='无法访问'):
        vault.get('test')
    assert outside.read_text() == 'do not read or change'


def test_native_vault_remains_keychain_by_default(monkeypatch):
    monkeypatch.delenv('MEDIALINK_VAULT_BACKEND', raising=False)
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout='fake-value\n')
    monkeypatch.setattr('medialink.core.subprocess.run', run)
    assert Vault().get('example') == 'fake-value'
    assert commands[0][:2] == ['/usr/bin/security', 'find-generic-password']


def test_container_environment_defaults_do_not_override_saved_settings(monkeypatch, tmp_path):
    monkeypatch.setenv('MEDIALINK_OPENLIST_URL', 'http://openlist:5244')
    monkeypatch.setenv('MEDIALINK_OPENLIST_DB', '/openlist-data/data.db')
    monkeypatch.setenv('MEDIALINK_STRM_LOCAL', '/strm')
    monkeypatch.setenv('MEDIALINK_ENABLED_SEARCH_SOURCES', 'nyaa,telegram,nyaa')
    store = Store(tmp_path/'state.db')
    assert store.settings()['openlist_url'] == 'http://openlist:5244'
    assert store.settings()['strm_local'] == '/strm'
    assert store.settings()['enabled_search_sources'] == ['nyaa', 'telegram']
    store.configure({'openlist_url': 'http://custom:5244', 'enabled_search_sources': []})
    assert Store(store.path).settings()['openlist_url'] == 'http://custom:5244'
    assert Store(store.path).settings()['enabled_search_sources'] == []
    assert DEFAULTS['openlist_url'] == 'http://127.0.0.1:5244'


def test_live_wal_database_is_readonly_and_sees_commits(tmp_path):
    path = tmp_path/'data with # characters.db'
    writer = sqlite3.connect(path)
    try:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        writer.execute('CREATE TABLE example(value TEXT)')
        writer.execute("INSERT INTO example VALUES ('first')")
        writer.commit()
        assert path.with_name(path.name+'-wal').exists()
        assert path.with_name(path.name+'-shm').exists()
        with readonly_database(path) as reader:
            assert reader.execute('SELECT value FROM example').fetchone()[0] == 'first'
            writer.execute("UPDATE example SET value='second'")
            writer.commit()
            assert reader.execute('SELECT value FROM example').fetchone()[0] == 'second'
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                reader.execute("UPDATE example SET value='forbidden'")
    finally:
        writer.close()


def test_cli_password_matches_http_login_and_does_not_allow_web_setup(monkeypatch, tmp_path):
    vault = file_vault(monkeypatch, tmp_path)
    monkeypatch.setenv('MEDIALINK_SETUP_MODE', 'cli')
    store = Store(tmp_path/'state.db')
    with TestClient(create_app(store, vault, start_workers=False)) as client:
        assert client.get('/api/v1/auth/state').json()['setup_mode'] == 'cli'
        assert client.post('/api/v1/auth/setup', json={'password': 'fake-password-123'}).status_code == 403
        assert not vault.get('password')
        answers = iter(['fake-password-123', 'fake-password-123'])
        initialize_password(vault, prompt=lambda _: next(answers))
        record = json.loads(vault.get('password'))
        assert record['hash'] == password_hash('fake-password-123', record['salt'])
        assert client.post('/api/v1/auth/login', json={'password': 'fake-password-123'}).status_code == 200
        assert client.get('/api/v1/settings').status_code == 200
        with pytest.raises(Problem, match='已设置'):
            initialize_password(vault, prompt=lambda _: pytest.fail('must not prompt again'))


@pytest.mark.parametrize('password,confirmation', [('short', 'short'), ('abcdefgh', 'different'), ('a'*201, 'a'*201)])
def test_cli_rejects_bad_password_without_saving(monkeypatch, tmp_path, password, confirmation):
    vault = file_vault(monkeypatch, tmp_path)
    answers = iter([password, confirmation])
    with pytest.raises(Problem):
        initialize_password(vault, prompt=lambda _: next(answers))
    assert vault.get('password') is None
