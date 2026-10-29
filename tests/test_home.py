import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from medialink.app import create_app
from medialink.core import SEARCH_SOURCE_IDS, Problem, Store
from medialink.home import LOCAL_KEY, LOCAL_MESSAGE, LOCAL_TIMEOUT_MESSAGE, HomeFeed, base_title, match_tmdb
from test_medialink import MemoryVault, authenticate


def media(type_, id_, title, year=None, **extra):
    return {"type": type_, "id": id_, "title": title, "year": year, **extra}

def test_match_tmdb_titles_types_seasons_and_years():
    movie = {"kind": "movie", "title": "魔法情缘", "year": 2026}
    assert match_tmdb(movie, [media("tv", 1, "魔法情缘", 2026), media("movie", 2, "魔法情缘", 1998),
                              media("movie", 3, "魔法情缘", 2026)]) == {"type": "movie", "id": 3}
    english = {"kind": "movie", "title": "蜘蛛侠：崭新之日", "english_title": "Spider-Man: Brand New Day", "year": 2026}
    assert match_tmdb(english, [media("movie", 9, "蜘蛛侠：英雄归来", 2026),
                                media("movie", 4, "Spider-Man: Brand New Day", 2026)]) == {"type": "movie", "id": 4}
    season = {"kind": "tv", "title": "足球教练 第四季", "year": 2026}
    assert base_title(season["title"]) == "足球教练" and base_title("仙逆 年番3") == "仙逆"
    assert match_tmdb(season, [media("tv", 5, "足球教练", 2020)]) == {"type": "tv", "id": 5}
    anime = {"kind": "anime", "title": "七龙珠超：比鲁斯", "year": 2026}
    assert match_tmdb(anime, [media("movie", 6, "七龙珠超：比鲁斯", 2025)]) == {"type": "movie", "id": 6}
    assert match_tmdb(movie, [media("movie", 7, "魔法", 2026), media("movie", 8, "魔法情缘", 2030)]) is None
    assert [base_title(t) for t in ("薬屋のひとりごと 第2期", "Dr.STONE 2nd Season", "某番 第2クール", "The Season")] == [
        "薬屋のひとりごと", "Dr.STONE", "某番", "The Season"]
    original = {"kind": "tv", "title": "用循环利息强制收回借出的魔力", "year": 2026,
                "names": ["貸した魔力は【リボ払い】で強制徴収 第2期", "魔", "Magic Repo Man"]}
    found = {**media("tv", 10, "借出去的魔力", 2026), "original_title": "貸した魔力は【リボ払い】で強制徴収"}
    assert match_tmdb(original, [media("tv", 11, "魔", 2026), found]) == {"type": "tv", "id": 10}
    assert match_tmdb({**original, "names": ["Magic Repo Man"]}, [{**media("tv", 12, "x", 2026),
                                                                  "english_title": "Magic Repo Man"}])["id"] == 12
    assert match_tmdb({**original, "names": []}, [found]) is None
    remake = {"kind": "tv", "title": "Keroro军曹☆", "year": 2026, "names": ["ケロロ軍曹☆"]}
    assert match_tmdb(remake, [{**media("tv", 13, "青蛙军曹", 2004), "original_title": "ケロロ軍曹"},
                               {**media("tv", 14, "Keroro", 2026), "original_title": "ケロロ軍曹☆"}])["id"] == 14
    assert match_tmdb(remake, [{**media("tv", 13, "青蛙军曹", 2004), "original_title": "ケロロ軍曹"},
                               {**media("tv", 15, "Keroro", None), "original_title": "ケロロ軍曹"}])["id"] == 15


def snapshot():
    found = {"id": "movie:42", "kind": "movie", "title": "示例影片", "media": {"type": "movie", "id": 42},
             "counts": {"magnet": 2, "cloud": 1, "online": 0}, "status": None}
    return {"origin": "local", "featured": [found], "sections": [
        {"key": "movie", "title": "最近更新的电影", "items": [found]}], "errors": []}


@pytest.fixture
def feed(tmp_path):
    local = AsyncMock()
    local.home.side_effect = snapshot
    return HomeFeed(Store(tmp_path / "state.db"), local)


async def test_refresh_reuses_cache_and_preserves_linked_titles(feed):
    empty = await feed.feed()
    assert empty["source"] == "local" and empty["status"] == "ready" and empty["message"] == LOCAL_MESSAGE
    assert empty["updated"] is None and empty["sections"] == []
    await feed.refresh()
    result = await feed.feed()
    assert result["featured"][0]["media"] == {"type": "movie", "id": 42}
    assert result["updated"] and not result["stale"] and not result["refreshing"]
    await feed.refresh()
    assert feed.local.home.await_count == 1
    await feed.refresh(force=True)
    assert feed.local.home.await_count == 2
    assert feed.store.cached_metadata(LOCAL_KEY)["data"]["origin"] == "local"


async def test_failed_refresh_preserves_stale_results_and_recovers(feed):
    await feed.refresh()
    feed.local.home.side_effect = Problem("来源暂时不可用", 502)
    with pytest.raises(Problem):
        await feed.refresh(force=True)
    result = await feed.feed()
    assert result["stale"] and result["error"] == "来源暂时不可用" and result["sections"][0]["items"]
    assert feed.retry_at > time.time()
    feed.local.home.side_effect = snapshot
    await feed.refresh(force=True)
    assert not (await feed.feed())["stale"] and feed.error is None and feed.retry_at == 0


async def test_timeout_preserves_cache_and_reports_retry(feed):
    await feed.refresh()
    feed.local.home.side_effect = TimeoutError
    with pytest.raises(Problem, match=LOCAL_TIMEOUT_MESSAGE):
        await feed.refresh(force=True)
    assert (await feed.feed())["stale"] and feed.retry_at > time.time()


async def test_partial_source_warnings_survive_the_cache(feed):
    data = snapshot()
    data["errors"] = ["某个来源暂时不可用"]
    feed.local.home.side_effect = None
    feed.local.home.return_value = data
    await feed.refresh()
    result = await feed.feed()
    assert result["warnings"] == data["errors"] and not result["stale"]


async def test_scheduled_refresh_obeys_retry_backoff(feed, monkeypatch):
    async def stop(_):
        feed.stopped = True

    monkeypatch.setattr(asyncio, "sleep", stop)
    feed.retry_at = time.time() + 60
    await feed.run()
    feed.local.home.assert_not_awaited()
    feed.stopped, feed.retry_at = False, 0
    await feed.run()
    feed.local.home.assert_awaited_once()


def test_old_settings_ignore_unsupported_keys_and_sources_without_deleting_data(tmp_path):
    store = Store(tmp_path / "state.db")
    store.configure({"retired_url": "https://retired.example", "home_source": "retired", "proxy": "", "115_account_verified": "test-account",
                     "enabled_search_sources": ["retired", "nyaa", "telegram", "nyaa"]})
    settings = store.settings()
    assert "retired_url" not in settings and "home_source" not in settings
    assert settings["enabled_search_sources"] == ["nyaa", "telegram"]
    assert settings["115_account_verified"] == "test-account"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM settings WHERE key='retired_url'").fetchone()[0] == 1
    app = create_app(store, MemoryVault(), start_workers=False)
    with TestClient(app) as client:
        authenticate(client)
        assert client.patch('/api/v1/settings', json={"yts_url": "https://yts.example"}).status_code == 200
        assert client.patch('/api/v1/settings', json={"115_account_verified": "other-account"}).status_code == 422
        assert client.post('/api/v1/search', json={"query": "test", "sources": ["retired"]}).status_code == 422


def test_home_api_requires_authentication_and_keeps_the_independent_feed(tmp_path):
    store = Store(tmp_path / "state.db")
    app = create_app(store, MemoryVault(), start_workers=False)
    app.state.home.local.home = AsyncMock(side_effect=snapshot)
    with TestClient(app) as client:
        assert client.get('/api/v1/home').status_code == 401
        authenticate(client)
        assert [source['id'] for source in client.get('/api/v1/sources').json()] == list(SEARCH_SOURCE_IDS)
        assert client.get('/api/v1/home').json()["source"] == "local"
        assert client.post('/api/v1/home/refresh', headers={"Origin": "https://evil.example"}).status_code == 403
        refreshed = client.post('/api/v1/home/refresh').json()
        assert refreshed["updated"] and refreshed["featured"][0]["media"]["id"] == 42
        assert not refreshed["stale"]
        assert client.patch('/api/v1/settings', json={"home_source": "other"}).status_code == 422
        assert client.patch('/api/v1/settings', json={"yts_url": "ftp://yts.example"}).status_code == 422
        app.state.home.error = "旧错误"
        saved = client.patch('/api/v1/settings', json={"eztv_url": "https://eztv.example"})
        assert saved.status_code == 200 and app.state.home.error is None
