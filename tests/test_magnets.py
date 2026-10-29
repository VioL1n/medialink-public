import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from medialink.app import MagnetSelection
from medialink.core import Problem, Store
from medialink.magnets import MagnetPreviews, preview_snapshot, safe_relative_path
from test_medialink import MAGNET, authenticate

pytest_plugins = ["test_medialink"]
HASH = "a" * 40
FILES = [{"path": "合集/季一/第一集.mkv", "size": 1234}, {"path": "合集/季二/第一集.mkv", "size": 2345},
         {"path": "合集/季一/第一集.zh.srt", "size": 30}, {"path": "合集/README.txt", "size": 12}]


def snapshot(files=FILES):
    return preview_snapshot(HASH, "合集", files)


@pytest.mark.parametrize("path", ["../movie.mkv", "/movie.mkv", "dir/../movie.mkv", "dir//movie.mkv", "dir/./movie.mkv", "a\\b.mkv", "a\x00.mkv", "a/" * 65 + "b.mkv", "a" * 2001])
def test_invalid_paths(path):
    with pytest.raises(Problem, match="不安全"):
        safe_relative_path(path)


def test_snapshot_names_and_limits():
    data = snapshot()
    assert data["root"] == "合集"
    assert [file["kind"] for file in data["files"]] == ["video", "video", "subtitle", "other"]
    assert data["files"][0]["id"] != data["files"][1]["id"]
    assert data == snapshot()
    assert preview_snapshot(HASH, "movie.mkv", [{"path": "movie.mkv", "size": 0}])["root"] is None
    for files in [[], FILES + FILES, [{"path": "a.mkv", "size": 0}, {"path": "a.mkv/b.mkv", "size": 1}], [{"path": "a.mkv", "size": -1}], [{"path": f"{i}.mkv", "size": 0} for i in range(20001)]]:
        with pytest.raises(Problem):
            snapshot(files)


async def test_cache_merge_and_waiter_cancellation(store):
    previews = MagnetPreviews(store)
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(link, key):
        entered.set()
        await release.wait()
        return snapshot()

    previews.resolve = AsyncMock(side_effect=resolve)
    first = asyncio.create_task(previews.preview(MAGNET))
    await entered.wait()
    second = asyncio.create_task(previews.preview(MAGNET + "&dn=other"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == snapshot()
    assert await previews.preview(MAGNET) == snapshot()
    previews.resolve.assert_awaited_once()
    assert not previews.pending
    assert Store(store.path).cached_metadata(previews.cache_key("magnet:" + HASH))["data"] == snapshot()


async def test_waiter_timeout_keeps_resolving_and_retry_resumes(store):
    previews = MagnetPreviews(store, timeout=0.02)
    release, calls = asyncio.Event(), []

    async def resolve(link, key):
        calls.append(key)
        await release.wait()
        return snapshot()

    previews.resolve = resolve
    with pytest.raises(Problem, match="后台解析") as raised:
        await previews.preview(MAGNET)
    assert raised.value.code == 504 and raised.value.headers == {"Retry-After": "0"}
    waiter = asyncio.create_task(previews.preview(MAGNET))
    await asyncio.sleep(0.005)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert "magnet:" + HASH in previews.pending
    previews.timeout = 60
    retry = asyncio.create_task(previews.preview(MAGNET))
    await asyncio.sleep(0.005)
    release.set()
    assert await retry == snapshot()
    assert calls == ["magnet:" + HASH] and not previews.pending
    assert previews.cached("magnet:" + HASH) == snapshot()


async def test_budget_exhaustion_cleans_up_without_cache(store):
    previews = MagnetPreviews(store, budget=0.02)
    cleaned = asyncio.Event()

    async def resolve(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    previews.resolve = resolve
    with pytest.raises(Problem, match="解析超时") as raised:
        await previews.preview(MAGNET)
    assert raised.value.code == 502 and raised.value.headers is None
    assert cleaned.is_set() and previews.cached("magnet:" + HASH) is None and not previews.pending


async def test_queue_wait_does_not_consume_budget(store):
    previews = MagnetPreviews(store, budget=0.1)
    release = asyncio.Event()

    async def resolve(link, key):
        if key.endswith("1" * 40):
            await asyncio.sleep(0.06)
            return snapshot()
        await release.wait()
        return snapshot()

    previews.resolve = resolve
    blockers = [asyncio.create_task(previews.preview("magnet:?xt=urn:btih:" + str(i) * 40)) for i in (2, 3, 4)]
    await asyncio.sleep(0.01)
    queued = asyncio.create_task(previews.preview("magnet:?xt=urn:btih:" + "1" * 40))
    await asyncio.sleep(0.08)
    release.set()
    assert await queued == snapshot()
    await asyncio.gather(*blockers, return_exceptions=True)


async def test_close_cancels_background_resolution(store):
    previews = MagnetPreviews(store, timeout=0.01)
    cleaned = asyncio.Event()

    async def resolve(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    previews.resolve = resolve
    with pytest.raises(Problem, match="后台解析"):
        await previews.preview(MAGNET)
    assert not cleaned.is_set()
    await previews.close()
    assert cleaned.is_set() and not previews.pending


def test_default_trackers_merge_after_link_trackers():
    from medialink.magnets import DEFAULT_TRACKERS, MAX_TRACKERS, magnet_trackers

    own = "udp://tracker.example.org:6969/announce"
    link = "magnet:?xt=urn:btih:" + HASH + "&tr=" + own + "&tr=" + DEFAULT_TRACKERS[0] + "&tr=ftp://bad.example/a&tr=" + own
    trackers = magnet_trackers(link)
    assert trackers[0] == own and trackers.count(own) == 1 and trackers.count(DEFAULT_TRACKERS[0]) == 1
    assert set(DEFAULT_TRACKERS) <= set(trackers) and not any(url.startswith("ftp:") for url in trackers)
    many = "magnet:?xt=urn:btih:" + HASH + "".join(f"&tr=udp://t{i}.example:1/announce" for i in range(40))
    assert len(magnet_trackers(many)) == MAX_TRACKERS


async def test_at_most_three_different_resolutions(store):
    previews = MagnetPreviews(store)
    active, peak = 0, 0
    release = asyncio.Event()

    async def resolve(link, key):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1
        return snapshot()

    previews.resolve = resolve
    tasks = [asyncio.create_task(previews.preview("magnet:?xt=urn:btih:" + str(i) * 40)) for i in range(1, 6)]
    await asyncio.sleep(0.01)
    assert active == 3
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 3


def test_preview_api_and_selection_validation_without_downloads(client, store):
    authenticate(client)
    previews = client.app.state.magnets
    previews.resolve = AsyncMock(return_value=snapshot())
    client.app.state.openlist.offline = AsyncMock()
    response = client.post("/api/v1/magnets/preview", json={"link": MAGNET})
    assert response.status_code == 200
    data = response.json()
    assert not store.tasks()
    client.app.state.openlist.offline.assert_not_awaited()
    assert client.post("/api/v1/magnets/preview", json={"link": "https://115.com/s/example"}).status_code == 422
    request = {"link": MAGNET, "target": "/115/infuse", "magnet_selection": {"preview_id": data["preview_id"], "selected_ids": [data["files"][0]["id"], data["files"][2]["id"]]}}
    created = client.post("/api/v1/tasks", json=request)
    assert created.status_code == 200
    task = created.json()
    assert task["selection"] == {"selected_count": 2, "retained_count": 2, "download_staging": None}
    saved = Store(store.path).task(task["id"])["payload"]["magnet_selection"]
    assert [file["path"] for file in saved["files"]] == [FILES[0]["path"], FILES[2]["path"]]
    # An identical retry remains idempotent even when the preview cache expires.
    store.cache_metadata(previews.cache_key("magnet:" + HASH), data, -1)
    assert client.post("/api/v1/tasks", json=request).json()["id"] == task["id"]
    different = {**request, "magnet_selection": {**request["magnet_selection"], "selected_ids": [data["files"][1]["id"]]}}
    assert client.post("/api/v1/tasks", json=different).status_code == 409
    assert client.post("/api/v1/tasks", json={"link": MAGNET, "target": "/115/infuse"}).status_code == 409
    assert len(store.tasks()) == 1
    # Validate new submissions before creating any download task.
    request["target"] = "/115/infuse/other"
    assert client.post("/api/v1/tasks", json=request).status_code == 409
    store.cache_metadata(previews.cache_key("magnet:" + HASH), data, 86400)
    for ids in [[], ["f" * 24], [data["files"][2]["id"]]]:
        request["magnet_selection"]["selected_ids"] = ids
        assert client.post("/api/v1/tasks", json=request).status_code == 422
    request["magnet_selection"]["preview_id"] = "b" * 64
    assert client.post("/api/v1/tasks", json=request).status_code == 409
    assert len(store.tasks()) == 1
    client.app.state.openlist.offline.assert_not_awaited()


def test_selection_rejects_mismatching_infohash_cache(store):
    previews = MagnetPreviews(store)
    data = snapshot()
    store.cache_metadata(previews.cache_key("magnet:" + HASH), data, 86400)
    with pytest.raises(Problem, match="失效"):
        previews.select("magnet:" + "b" * 40, MagnetSelection(preview_id=data["preview_id"], selected_ids=[data["files"][0]["id"]]))


async def test_real_libtorrent_local_metadata_only(store, tmp_path, monkeypatch):
    import libtorrent as lt

    seed_path = tmp_path / "seed"
    seed_path.mkdir()
    (seed_path / "测试.mkv").write_bytes(b"test payload" * 2048)
    files = lt.file_storage()
    files.add_file("测试.mkv", (seed_path / "测试.mkv").stat().st_size)
    torrent = lt.create_torrent(files, 16384, flags=lt.create_torrent.v1_only)
    lt.set_piece_hashes(torrent, str(seed_path))
    info = lt.torrent_info(torrent.generate())
    base_settings = {"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
                     "enable_upnp": False, "enable_natpmp": False}
    seed = lt.session(base_settings)
    params = lt.add_torrent_params()
    params.ti, params.save_path = info, str(seed_path)
    params.flags |= lt.torrent_flags.seed_mode
    params.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
    seed_handle = seed.add_torrent(params)
    for _ in range(100):
        if seed.listen_port() and seed_handle.status().is_seeding:
            break
        await asyncio.sleep(0.05)
    assert seed.listen_port() and seed_handle.status().is_seeding
    original_session = lt.session
    observed = {"sessions": 0}

    class LocalSession:
        def __init__(self, settings):
            observed["sessions"] += 1
            settings.settings = {**settings.settings, **base_settings}
            self.session = original_session(settings)

        def add_torrent(self, params):
            observed["path"] = params.save_path
            observed["flags"] = params.flags
            handle = self.session.add_torrent(params)
            handle.connect_peer(("127.0.0.1", seed.listen_port()))
            return handle

        def remove_torrent(self, handle):
            observed["payload_bytes"] = handle.status().total_payload_download
            observed["payload_files"] = [str(path) for path in Path(observed["path"]).rglob("*") if path.is_file()]
            self.session.remove_torrent(handle)

        def pause(self):
            self.session.pause()

    monkeypatch.setattr(lt, "session", LocalSession)
    previews = MagnetPreviews(store, timeout=10)
    try:
        data = await previews.preview("magnet:?xt=urn:btih:" + str(info.info_hash()))
        assert data["files"][0]["path"] == "测试.mkv"
        assert data["files"][0]["size"] == 24576
        assert observed["flags"] & lt.torrent_flags.upload_mode
        assert not Path(observed["path"]).exists()
        assert observed["payload_bytes"] == 0 and not observed["payload_files"]
        store.cache_metadata(previews.cache_key("magnet:" + str(info.info_hashes().v1)), data, -1)
        assert await previews.preview("magnet:?xt=urn:btih:" + str(info.info_hashes().v1)) == data
        assert observed["sessions"] == 1
        assert observed["payload_bytes"] == 0 and not observed["payload_files"]
    finally:
        await previews.close()
        seed.remove_torrent(seed_handle)
        seed.pause()


@pytest.mark.parametrize("issue", ["hash", "path", "count", "symlink", "timeout", "padding"])
async def test_resolver_validation_and_cleanup(store, monkeypatch, issue):
    from types import SimpleNamespace

    import libtorrent as lt

    cleaned = []

    class Storage:
        def num_files(self):
            return 20001 if issue == "count" else 2

        def file_flags(self, index):
            return lt.file_storage.flag_symlink if issue == "symlink" else lt.file_storage.flag_pad_file if index == 1 else 0

        def file_path(self, index):
            return "../movie.mkv" if issue == "path" else "片子/movie.mkv"

        def file_size(self, index):
            return 100

    info = SimpleNamespace(info_hashes=lambda: SimpleNamespace(v1="b" * 40 if issue == "hash" else HASH),
                           files=Storage, name=lambda: "片子")
    handle = SimpleNamespace(torrent_file=lambda: None if issue == "timeout" else info,
                             status=lambda: SimpleNamespace(errc=SimpleNamespace(value=lambda: 0)))

    class Session:
        def __init__(self, settings):
            pass

        def add_torrent(self, params):
            return handle

        def remove_torrent(self, torrent):
            cleaned.append(torrent)

        def pause(self):
            pass

    monkeypatch.setattr(lt, "session", Session)
    previews = MagnetPreviews(store, budget=0.02)
    if issue == "padding":
        data = await previews.preview(MAGNET)
        assert len(data["files"]) == 1 and data["files"][0]["path"] == "片子/movie.mkv"
    else:
        with pytest.raises(Problem):
            await previews.preview(MAGNET)
        assert previews.cached("magnet:" + HASH) is None
    assert cleaned == [handle]
    await previews.close()


async def test_warm_session_survives_cancellation_and_is_reused(store, monkeypatch):
    from types import SimpleNamespace

    import libtorrent as lt

    sessions, removed = [], []
    entered = asyncio.Event()

    class Session:
        def __init__(self, params):
            self.params, self.paused = params, False
            sessions.append(self)

        def add_torrent(self, params):
            entered.set()
            return SimpleNamespace(torrent_file=lambda: None,
                                   status=lambda: SimpleNamespace(errc=SimpleNamespace(value=lambda: 0)))

        def remove_torrent(self, handle):
            removed.append(handle)

        def session_state(self, flags):
            return self.params

        def pause(self):
            self.paused = True

    monkeypatch.setattr(lt, "session", Session)
    previews = MagnetPreviews(store, budget=0.03)
    await previews.warmup()
    assert len(sessions) == 1 and previews.engines[""]["users"] == 0
    settings = sessions[0].params.settings
    assert settings["announce_to_all_trackers"] and settings["announce_to_all_tiers"]
    assert settings["connections_limit"] == 120 and settings["peer_connect_timeout"] == 7
    first = asyncio.create_task(previews.preview(MAGNET))
    await entered.wait()
    background = previews.pending["magnet:" + HASH]
    second = asyncio.create_task(previews.preview("magnet:?xt=urn:btih:" + "b" * 40))
    await asyncio.sleep(0.005)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not removed and not sessions[0].paused
    with pytest.raises(Problem, match="解析超时"):
        await second
    with pytest.raises(Problem, match="解析超时"):
        await background
    assert len(sessions) == 1 and len(removed) == 2 and not sessions[0].paused
    await previews.close()
    assert sessions[0].paused and not previews.engines


async def test_dht_persistence_excludes_settings_and_restores_without_proxy_secrets(store, monkeypatch):
    import libtorrent as lt

    sessions, loaded = [], []
    read_params = lt.read_session_params
    store.configure({"proxy": "socks5://username:secret@127.0.0.1:1080", "magnet_use_proxy": True})

    class Session:
        def __init__(self, params):
            self.params = params
            sessions.append(self)

        def session_state(self, flags):
            return self.params

        def pause(self):
            pass

    def read_state(buffer, flags):
        loaded.append(buffer)
        return read_params(buffer, flags)

    monkeypatch.setattr(lt, "session", Session)
    monkeypatch.setattr(lt, "read_session_params", read_state)
    previews = MagnetPreviews(store)
    await previews.warmup()
    key = next(iter(previews.engines.values()))["cache_key"]
    await previews.close()
    cached = store.cached_metadata(key)
    encoded = bytes.fromhex(cached["data"]["state"])
    assert b"username" not in encoded and b"secret" not in encoded
    assert b"settings" not in lt.bdecode(encoded)
    restarted = MagnetPreviews(Store(store.path))
    await restarted.warmup()
    assert loaded == [encoded]
    assert sessions[-1].params.settings["proxy_password"] == "secret"
    await restarted.close()


async def test_proxy_changes_keep_busy_session_and_discard_idle_session(store, monkeypatch):
    import libtorrent as lt

    class Session:
        def __init__(self, params):
            self.params, self.paused = params, False

        def session_state(self, flags):
            return self.params

        def pause(self):
            self.paused = True

    monkeypatch.setattr(lt, "session", Session)
    store.configure({"magnet_use_proxy": True})
    previews = MagnetPreviews(store)
    original = await previews._engine()
    store.configure({"proxy": "socks5://127.0.0.1:1080"})
    changed = await previews._engine()
    assert changed is not original and not original["session"].paused
    original["users"] -= 1
    changed["users"] -= 1
    store.configure({"proxy": "http://127.0.0.1:8888"})
    replacement = await previews._engine()
    replacement["users"] -= 1
    assert original["session"].paused and changed["session"].paused
    assert len(previews.engines) == 1
    await previews.close()


@pytest.mark.parametrize("use_proxy", [False, True])
async def test_magnet_proxy_switch(store, monkeypatch, use_proxy):
    import libtorrent as lt

    sessions = []
    monkeypatch.setattr(lt, "session", lambda params: sessions.append(params) or SimpleNamespace(pause=lambda: None))
    store.configure({"proxy": "socks5://127.0.0.1:1080", "magnet_use_proxy": use_proxy})
    previews = MagnetPreviews(store)
    await previews.warmup()
    assert list(previews.engines) == ["socks5://127.0.0.1:1080" if use_proxy else ""]
    assert ("proxy_hostname" in sessions[0].settings) is use_proxy
    await previews.close()


async def test_dht_state_saves_are_throttled(store, monkeypatch):
    saves = []
    previews = MagnetPreviews(store)
    monkeypatch.setattr(previews.store, "cache_metadata", lambda *args: saves.append(args))
    monkeypatch.setattr("libtorrent.write_session_params_buf", lambda state, flags: b"state")
    engine = {"session": SimpleNamespace(session_state=lambda flags: None), "cache_key": "magnet-dht:x", "saved": 0}
    previews._save_engine(engine)
    previews._save_engine(engine)
    assert len(saves) == 1
    previews._save_engine(engine, force=True)
    assert len(saves) == 2


async def test_invalid_saved_dht_state_does_not_block_warmup(store, monkeypatch):
    import hashlib

    import libtorrent as lt

    captured = []
    store.cache_metadata("magnet-dht:" + hashlib.sha256(b"").hexdigest(), {"state": "not hex"}, 86400)
    from types import SimpleNamespace

    monkeypatch.setattr(lt, "session", lambda params: captured.append(params) or SimpleNamespace(pause=lambda: None))
    previews = MagnetPreviews(store)
    await previews.warmup()
    assert len(captured) == 1 and previews.engines[""]["users"] == 0
    await previews.close()
