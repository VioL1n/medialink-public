import runpy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from medialink import core
from medialink.clients import refresh_emby


def test_native_default_paths_follow_the_current_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "portable-user"))
    monkeypatch.delenv("MEDIALINK_DATA", raising=False)
    namespace = runpy.run_path(core.__file__)
    assert namespace["DEFAULTS"]["strm_local"] == str(tmp_path / "portable-user/Applications/openlist/strm")
    assert namespace["DATA"] == tmp_path / "portable-user/Library/Application Support/MediaLink"


@pytest.mark.parametrize("user", [None, "launchagent-user"])
async def test_emby_keychain_fallback_uses_current_user(monkeypatch, tmp_path, user):
    if user is None:
        monkeypatch.delenv("USER", raising=False)
    else:
        monkeypatch.setenv("USER", user)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "portable-user"))
    calls = []

    class FakeVault:
        def get(self, name, service=None):
            calls.append((name, service))
            return None if name == "emby-token" else "FAKE-ISOLATED-KEY"

    client = AsyncMock()
    client.post.return_value = httpx.Response(200, request=httpx.Request("POST", "http://emby.test/Library/Refresh"))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("medialink.clients.httpx.AsyncClient", lambda **kwargs: context)
    await refresh_emby({"emby_url": "http://emby.test", "emby_keychain_service": "test.service"}, FakeVault())
    assert calls == [("emby-token", None), (user or "portable-user", "test.service")]
