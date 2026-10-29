import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from medialink.clients import OpenList, RemoteFailure
from medialink.core import Store
from test_medialink import MemoryVault


def test_store_commits_and_closes_each_connection(tmp_path):
    store = Store(tmp_path / "state.db")
    with store.connect() as connection:
        connection.execute("INSERT INTO settings VALUES ('poll_seconds', '123')")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    assert store.settings()["poll_seconds"] == 123


def test_store_rolls_back_and_closes_on_failure(tmp_path):
    store = Store(tmp_path / "state.db")
    with pytest.raises(RuntimeError, match="abort"):
        with store.connect() as connection:
            connection.execute("INSERT INTO settings VALUES ('poll_seconds', '123')")
            raise RuntimeError("abort")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    assert store.settings()["poll_seconds"] == 30


def test_store_closes_connection_when_initialization_fails(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    connection = MagicMock()
    connection.execute.side_effect = sqlite3.OperationalError("pragma failed")
    monkeypatch.setattr("medialink.core.sqlite3.connect", lambda *args, **kwargs: connection)
    with pytest.raises(sqlite3.OperationalError, match="pragma failed"):
        with store.connect():
            pytest.fail("connection setup should fail before entering the body")
    connection.close.assert_called_once()


def test_polling_closes_connections_without_waiting_for_gc(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    connect = sqlite3.connect
    connections = []

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)  # Retain references to prevent GC from masking leaks.
        return connection

    monkeypatch.setattr("medialink.core.sqlite3.connect", tracked_connect)
    for _ in range(150):
        assert store.tasks(active=True) == []
        assert store.settings()["poll_seconds"] == 30
    assert len(connections) == 300
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


@pytest.mark.parametrize("operation", ["token", "strm", "identity", "native"])
async def test_openlist_closes_readonly_database_connections(monkeypatch, operation):
    # sqlite3's own context manager commits/rolls back but deliberately does not close.
    connection = MagicMock()
    connection.__enter__.return_value = connection
    addition = {"access_token": "test-access", "root_folder_id": "123"}
    connection.execute.return_value.fetchone.return_value = (
        "test-token" if operation == "token" else json.dumps(addition),)
    connection.execute.return_value.fetchall.return_value = [("/115Strm", json.dumps({"paths": "/115/infuse"}))]
    monkeypatch.setattr("medialink.clients.sqlite3.connect", lambda *args, **kwargs: connection)
    ol = OpenList(lambda: {"openlist_db": "test.db", "mount_root": "/115"}, MemoryVault())
    if operation == "token":
        assert ol.token() == "test-token"
    elif operation == "strm":
        ol.get = AsyncMock(side_effect=[RemoteFailure("missing"), {"is_dir": True}])
        assert await ol.resolve_strm_path("/115Strm/infuse/movie") == "/115Strm/movie"
    elif operation == "native":
        assert await ol.native_status("a" * 40, "/115/staging") is None
    else:
        client = AsyncMock()
        client.get.return_value = httpx.Response(200, json={"state": True, "data": {"user_id": "42"}})
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr("medialink.clients.httpx.AsyncClient", lambda **kwargs: context)
        assert await ol.storage_identity() == {"user_id": "42", "root_id": "123"}
    connection.close.assert_called_once()
