import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from medialink.app import SearchRequest, create_app
from medialink.core import Problem, Store
from medialink.search_jobs import SearchEngine, rank_results
from medialink.tmdb import TMDB, media_queries, normalize_title, query_title
from test_medialink import MAGNET, MemoryVault, authenticate


def movie(mid=100, name="星际穿越", year=2014, english="Interstellar", original=None, kind="movie"):
    return {
        "id": mid,
        "type": kind,
        "title": name,
        "chinese_title": name,
        "english_title": english,
        "original_title": original or english,
        "year": year,
        "updated": time.time(),
        "stale": False,
        "seasons": [{"number": 0}, {"number": 1}, {"number": 2}] if kind == "tv" else [],
    }


def raw_movie(mid=100):
    return {
        "id": mid,
        "title": "星际穿越",
        "original_title": "Interstellar",
        "original_language": "en",
        "release_date": "2014-11-05",
        "poster_path": "/poster.jpg",
        "backdrop_path": "/backdrop.jpg",
        "vote_average": 8.6,
        "vote_count": 1000,
        "overview": "中文简介",
        "runtime": 169,
        "genres": [{"name": "科幻"}],
        "translations": {
            "translations": [
                {"iso_639_1": "zh", "iso_3166_1": "CN", "data": {"title": "星际穿越", "overview": "中文简介"}},
                {
                    "iso_639_1": "en",
                    "iso_3166_1": "US",
                    "data": {"title": "Interstellar", "overview": "English overview"},
                },
            ]
        },
        "credits": {
            "crew": [{"name": "Christopher Nolan", "job": "Director"}],
            "cast": [{"name": "Matthew McConaughey", "character": "Cooper", "profile_path": "/actor.jpg"}],
        },
    }


def resource(title="Interstellar 2014 1080p", suffix="a"):
    return {
        "key": "magnet:" + suffix * 40,
        "title": title,
        "kind": "magnet",
        "link": MAGNET.replace("a" * 40, suffix * 40),
        "password": "",
        "size": None,
        "resolution": "1080P",
        "published": None,
        "seeders": None,
        "sources": [{"provider": "knaben", "name": "Knaben", "url": "https://example.org/film"}],
    }


@pytest.fixture
def tmdb(tmp_path):
    vault = MemoryVault()
    vault.set("tmdb-credential", json.dumps({"type": "token", "value": "isolated-test-token"}))
    return TMDB(Store(tmp_path / "db.sqlite"), vault)


def transport(tmdb, handler):
    tmdb.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


async def test_tmdb_persistent_cache_and_token_header(tmdb):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer isolated-test-token"
        assert "api_key" not in request.url.params
        return httpx.Response(
            200,
            json={
                "results": [dict(raw_movie(), media_type="movie"), {"id": 10, "media_type": "person"}],
                "total_pages": 4,
            },
        )

    transport(tmdb, handler)
    response = await tmdb.trending()
    assert len(response["items"]) == 1 and response["items"][0]["title"] == "星际穿越"
    assert response["items"][0]["poster"] == "/api/v1/tmdb/images/w500/poster.jpg"
    other = TMDB(Store(tmdb.store.path), tmdb.vault)
    other.client = lambda: (_ for _ in ()).throw(AssertionError("network not needed"))
    assert await other.trending() == response
    assert len(calls) == 1
    with tmdb.store.connect() as db:
        assert "isolated-test-token" not in "".join(
            row[0] + row[1] for row in db.execute("SELECT key,data FROM metadata_cache")
        )


async def test_tmdb_api_key_and_safe_auth_error(tmdb):
    await tmdb.configure({"type": "api_key", "value": "isolated-test-key"})

    def handler(request):
        assert request.url.params["api_key"] == "isolated-test-key"
        assert "Authorization" not in request.headers
        return httpx.Response(401, text="isolated-test-key private-response")

    transport(tmdb, handler)
    with pytest.raises(Problem, match="凭据无效") as error:
        await tmdb.trending()
    assert "isolated-test-key" not in str(error.value) and "private-response" not in str(error.value)


async def test_tmdb_translation_original_is_not_english_and_overview_fallback(tmdb):
    raw = raw_movie(200)
    raw.update(title="你的名字。", original_title="君の名は。", original_language="ja", overview="")
    raw["translations"]["translations"][0]["data"] = {"title": "你的名字。", "overview": ""}
    raw["translations"]["translations"][1]["data"] = {"title": "Your Name.", "overview": "English synopsis"}
    transport(tmdb, lambda request: httpx.Response(200, json=raw))
    detail = await tmdb.detail("movie", 200)
    assert detail["english_title"] == "Your Name." and detail["original_title"] == "君の名は。"
    assert detail["overview"] == "English synopsis" and detail["overview_language"] == "en"
    assert detail["creators"] == ["Christopher Nolan"] and detail["cast"][0]["name"] == "Matthew McConaughey"
    assert media_queries("你的名字", detail) == ["你的名字", "Your Name.", "君の名は。"]


async def test_tmdb_missing_translation_does_not_invent_english(tmdb):
    raw = raw_movie()
    raw.update(original_language="ja", original_title="君の名は。", title="你的名字")
    raw["translations"] = {"translations": []}

    def handler(request):
        return httpx.Response(200, json=raw) if request.url.params["language"] == "zh-CN" else httpx.Response(503)

    transport(tmdb, handler)
    detail = await tmdb.detail("movie", 100)
    assert detail["english_title"] is None and detail["chinese_title"] == "你的名字"
    assert detail["warning"] == "部分英文资料暂不可用"


async def test_tmdb_two_languages_merge_candidates_but_not_media_types(tmdb):
    def handler(request):
        english = request.url.params["language"] == "en-US"
        hit = dict(raw_movie(), media_type="movie", title="Interstellar" if english else "星际穿越")
        tv = {
            "id": 100,
            "media_type": "tv",
            "name": "Interstellar" if english else "星际穿越",
            "first_air_date": "2020-01-01",
        }
        return httpx.Response(200, json={"results": [hit, tv, {"id": 100, "media_type": "person"}], "total_pages": 1})

    transport(tmdb, handler)
    result = await tmdb.search("星际穿越")
    assert len(result["items"]) == 2
    assert result["items"][0]["english_title"] == "Interstellar"
    assert result["items"][0]["title"] == "星际穿越"


@pytest.mark.parametrize("status", [429, 503])
async def test_tmdb_stale_cache_and_rate_limit(tmdb, status):
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(200, json={"results": [], "total_pages": 1})
            if len(calls) == 1
            else httpx.Response(status, headers={"Retry-After": "120"})
        )

    transport(tmdb, handler)
    first = await tmdb.trending()
    with tmdb.store.connect() as db:
        db.execute("UPDATE metadata_cache SET expires=0")
    fallback = await tmdb.trending()
    assert fallback["stale"] and fallback["updated"] == first["updated"]
    if status == 429:
        assert tmdb.wait_until > time.time() + 115
        assert (await tmdb.trending())["stale"] and len(calls) == 2
        with pytest.raises(Problem, match="限流"):
            await tmdb.trending("movie")


async def test_tmdb_connection_check_really_tests_new_credential(tmdb):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"success": True}) if len(calls) == 1 else httpx.Response(401)

    transport(tmdb, handler)
    assert (await tmdb.check())["ok"]
    await tmdb.configure({"type": "token", "value": "another-isolated-token"})
    with pytest.raises(Problem, match="凭据无效"):
        await tmdb.check()
    assert len(calls) == 2


@pytest.mark.parametrize(
    "size,path",
    [
        ("bad", "poster.jpg"),
        ("w500", "../poster.jpg"),
        ("w500", "https://evil.test/image.jpg"),
        ("w500", "folder/poster.jpg"),
        ("w500", "poster.svg"),
        ("w500", "poster.jpg?token=secret"),
    ],
)
async def test_tmdb_image_rejects_arbitrary_paths(tmdb, size, path):
    with pytest.raises(Problem, match="无效"):
        await tmdb.image(size, path)


async def test_tmdb_image_auth_boundary_cache_lru_and_no_credential_header(tmdb):
    calls = []

    def handler(request):
        calls.append(request)
        assert (
            request.url.host == "image.tmdb.org"
            and "Authorization" not in request.headers
            and "api_key" not in request.url.params
        )
        return httpx.Response(200, content=b"\xff\xd8test\xff\xd9", headers={"Content-Type": "image/jpeg"})

    transport(tmdb, handler)
    data, mime = await tmdb.image("w500", "poster.jpg")
    assert mime == "image/jpeg" and data.startswith(b"\xff\xd8")
    assert await tmdb.image("w500", "poster.jpg") == (data, mime) and len(calls) == 1
    tmdb.image_limit = len(data) + 1
    await tmdb.image("w500", "another.jpg")
    assert len(list(tmdb.image_dir.glob("*.image"))) == 1
    app = create_app(tmdb.store, tmdb.vault, start_workers=False)
    app.state.tmdb = tmdb
    with TestClient(app) as client:
        assert client.get("/api/v1/tmdb/images/w500/poster.jpg").status_code == 401


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, text="<html>no image</html>", headers={"Content-Type": "text/html"}),
        httpx.Response(302, headers={"Location": "https://evil.test/image.jpg"}),
    ],
)
async def test_tmdb_image_html_and_redirect_are_rejected(tmdb, reply):
    transport(tmdb, lambda request: reply)
    with pytest.raises(Problem):
        await tmdb.image("w500", "poster.jpg")
    assert not tmdb.image_dir.exists()


@pytest.mark.parametrize(
    "value",
    [
        {"type": [], "value": "secret"},
        {"type": "unknown", "value": "secret"},
        {"type": "token", "value": "bad secret"},
        {"type": "token", "value": []},
        "secret",
        {"type": "token", "value": "", "extra": 1},
    ],
)
async def test_tmdb_bad_credentials_are_validation_errors(tmdb, value):
    with pytest.raises(Problem) as error:
        await tmdb.configure(value)
    assert error.value.code == 422


def test_tmdb_settings_save_clear_and_migration_preserve_tasks(tmdb):
    task = tmdb.store.create_task("magnet:" + "a" * 40, "/115/infuse", {"kind": "magnet", "title": "kept"})
    app = create_app(tmdb.store, tmdb.vault, start_workers=False)
    with TestClient(app) as client:
        authenticate(client)
        response = client.patch(
            "/api/v1/settings", json={"tmdb_credential": {"type": "api_key", "value": "private-test-value"}}
        )
        assert response.status_code == 200 and response.json()["tmdb_configured"]
        assert "private-test-value" not in response.text
        assert "tmdb_credential" not in tmdb.store.settings()
        response = client.patch("/api/v1/settings", json={"tmdb_credential": None})
        assert not response.json()["tmdb_configured"] and not tmdb.vault.get("tmdb-credential")
        assert client.get("/api/v1/tmdb/trending").status_code == 503
        assert (
            client.post(
                "/api/v1/search", json={"query": "x", "media": {"type": "movie", "id": 100, "season_number": 1}}
            ).status_code
            == 422
        )
    assert Store(tmdb.store.path).task(task["id"])["payload"]["title"] == "kept"


def test_tmdb_normalization_year_and_season_variants():
    assert normalize_title(" ＳＰＩＤＥＲ—ＭＡＮ ") == "spiderman"
    assert query_title("星际穿越（2014）") == ("星际穿越", 2014)
    assert query_title("1917") == ("1917", None)
    show = movie(name="怪奇物语", english="Stranger Things", kind="tv")
    assert media_queries("怪奇物语", show, 2) == ["怪奇物语 第二季", "怪奇物语 第2季", "Stranger Things S02"]


def engine_for(tmdb, candidates=None, selected=None):
    tmdb.search = AsyncMock(return_value={"items": candidates or [], "updated": 1, "stale": False})
    tmdb.detail = AsyncMock(return_value=selected or movie())
    source = type("Source", (), {"search": AsyncMock(return_value=[resource()])})()
    return SearchEngine({"knaben": source}, tmdb), source


@pytest.mark.parametrize(
    "candidates,query,status",
    [
        ([movie()], "星际穿越", "resolved"),
        ([movie(), movie(101, year=2020)], "星际穿越", "candidates"),
        ([movie(), movie(101, year=2020)], "星际穿越 2014", "resolved"),
        ([movie(), movie(100, kind="tv")], "星际穿越", "candidates"),
        ([movie()], "穿越", "candidates"),
        ([], "不存在", "no_match"),
    ],
)
async def test_tmdb_exact_unique_resolution_requires_unambiguous_title(tmdb, candidates, query, status):
    engine, _ = engine_for(tmdb, candidates)
    result = await engine.resolve(SearchRequest(query=query, sources=["knaben"]))
    assert result["status"] == status
    assert tmdb.detail.await_count == (1 if status == "resolved" else 0)


async def test_tmdb_search_expansion_streaming_dedup_and_ranking(tmdb):
    engine, source = engine_for(tmdb, [movie()])
    job = {"events": [], "results": [], "done": False}
    await engine.execute(SearchRequest(query="星际穿越", sources=["knaben"]), job)
    assert [call.args[0] for call in source.search.await_args_list] == ["星际穿越", "Interstellar"]
    assert job["done"] and len(job["results"]) == 1
    assert job["results"][0]["matched_queries"] == ["星际穿越", "Interstellar"]
    assert job["results"][0]["match"]["status"] == "matched"
    assert any(e.get("_event") == "metadata" and e["status"] == "resolved" for e in job["events"])
    assert [e["status"] for e in job["events"] if "provider" in e][-1] == "done"


async def test_tmdb_timeout_keeps_original_results(tmdb):
    engine, source = engine_for(tmdb, [movie()])
    engine.source_budget = 0.03

    async def handler(query):
        if query == "Interstellar":
            await asyncio.sleep(0.3)
        return [resource()]

    source.search.side_effect = handler
    job = {"events": [], "results": [], "done": False}
    await engine.execute(SearchRequest(query="星际穿越", sources=["knaben"]), job)
    assert len(job["results"]) == 1 and job["done"]
    assert job["events"][-1]["status"] == "done" and "超时" in job["events"][-1]["error"]


@pytest.mark.parametrize("failure", [Problem("TMDB unavailable", 502), TimeoutError()])
async def test_tmdb_unavailable_falls_back_to_original_search(tmdb, failure):
    engine, source = engine_for(tmdb)
    tmdb.search.side_effect = failure
    job = {"events": [], "results": [], "done": False}
    await engine.execute(SearchRequest(query="原词", sources=["knaben"]), job)
    assert source.search.await_args.args == ("原词",) and len(job["results"]) == 1
    assert job["metadata"]["status"] == "error"


async def test_tmdb_selected_season_uses_server_titles_and_season_list(tmdb):
    show = movie(kind="tv", name="怪奇物语", english="Stranger Things")
    engine, source = engine_for(tmdb, selected=show)
    job = {"events": [], "results": [], "done": False}
    await engine.execute(
        SearchRequest(query="untrusted input", sources=["knaben"], media={"type": "tv", "id": 100, "season_number": 2}),
        job,
    )
    assert [call.args[0] for call in source.search.await_args_list] == [
        "怪奇物语 第二季",
        "怪奇物语 第2季",
        "Stranger Things S02",
    ]
    assert tmdb.detail.await_args.args == ("tv", 100)
    resolved = await engine.resolve(SearchRequest(query="x", media={"type": "tv", "id": 100, "season_number": 999}))
    assert resolved["status"] == "error" and "这一季" in resolved["message"]


def test_tmdb_year_season_and_short_english_titles_rank_without_discarding():
    film = movie(name="小丑回魂", english="It", year=2017)
    result = rank_results([resource("Site 2017", "b"), resource("It 1990", "c"), resource("It 2017", "a")], film)
    assert result[0]["title"] == "It 2017" and len(result) == 3
    wrong_year = next(item for item in result if item["title"] == "It 1990")
    assert wrong_year["match"]["status"] == "review" and "年份不同" in wrong_year["match"]["explanation"]
    wrong_title = next(item for item in result if item["title"] == "Site 2017")
    assert wrong_title["match"]["status"] == "review" and "片名" in wrong_title["match"]["explanation"]
    show = movie(kind="tv", name="怪奇物语", english="Stranger Things")
    result = rank_results([resource("Stranger Things S01 2014", "b"), resource("怪奇物语 第二季", "a")], show, 2)
    assert result[0]["title"] == "怪奇物语 第二季" and result[0]["match"]["status"] == "matched"


def test_tmdb_sse_and_detail_resource_can_create_existing_task(tmdb):
    app = create_app(tmdb.store, tmdb.vault, start_workers=False)
    app.state.tmdb.detail = AsyncMock(return_value=movie())
    app.state.knaben.search = AsyncMock(return_value=[resource()])
    app.state.openlist.get = AsyncMock(return_value={"is_dir": True})
    with TestClient(app) as client:
        authenticate(client)
        job = client.post(
            "/api/v1/search", json={"query": "星际穿越", "sources": ["knaben"], "media": {"type": "movie", "id": 100}}
        ).json()
        events = client.get("/api/v1/search/" + job["id"] + "/events").text
        assert "event: metadata" in events and "Interstellar" in events
        selected = json.loads(events.split("event: done\ndata: ")[1].split("\n")[0])["results"][0]
        task = client.post(
            "/api/v1/tasks",
            json={
                "title": selected["title"],
                "link": selected["link"],
                "target": "/115/infuse",
                "sources": selected["sources"],
            },
        )
        assert task.status_code == 200 and task.json()["state"] == "queued"
        assert task.json()["sources"] == selected["sources"]


async def test_tmdb_latin_original_without_translation_is_not_english(tmdb):
    raw = raw_movie()
    raw.update(title="天使爱美丽", original_language="fr", original_title="Amélie")
    raw["translations"] = {"translations": []}

    def handler(request):
        localized = {**raw, "title": "Amélie"} if request.url.params["language"] == "en-US" else raw
        return httpx.Response(200, json=localized)

    transport(tmdb, handler)
    detail = await tmdb.detail("movie", 100)
    assert detail["english_title"] is None and detail["original_title"] == "Amélie"


def test_tmdb_numeric_film_title_is_not_a_release_year():
    assert query_title("2001太空漫游") == ("2001太空漫游", None)
    assert query_title("2001 A Space Odyssey") == ("2001 A Space Odyssey", None)
    film = movie(name="2001太空漫游", english="2001 A Space Odyssey", year=1968)
    ranked = rank_results([resource("2001 A Space Odyssey 1968 1080p")], film)
    assert ranked[0]["match"]["status"] == "matched"
    unknown = rank_results([resource("2001 A Space Odyssey 1080p")], film)
    assert "年份未知" in unknown[0]["match"]["explanation"]


async def test_tmdb_incomplete_candidate_pages_do_not_claim_unique_match(tmdb):
    engine, _ = engine_for(tmdb, [movie()])
    tmdb.search.return_value["total_pages"] = 2
    result = await engine.resolve(SearchRequest(query="星际穿越"))
    assert result["status"] == "candidates" and not tmdb.detail.await_count


async def test_tmdb_original_query_starts_before_metadata_and_empty_waits_for_all_variants(tmdb):
    engine, source = engine_for(tmdb, [movie()])
    entered, finish_metadata = asyncio.Event(), asyncio.Event()

    async def titles(query):
        await finish_metadata.wait()
        return {"items": [movie()], "updated": 1, "stale": False}

    async def original(query):
        entered.set()
        return [] if query == "星际穿越" else [resource()]

    tmdb.search.side_effect = titles
    source.search.side_effect = original
    job = {"events": [], "results": [], "done": False}
    task = asyncio.create_task(engine.execute(SearchRequest(query="星际穿越", sources=["knaben"]), job))
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.sleep(0)
    assert source.search.await_args_list[0].args == ("星际穿越",)
    assert not any(e.get("status") == "done" for e in job["events"])
    finish_metadata.set()
    await task
    assert job["events"][-1]["status"] == "done" and len(job["results"]) == 1


async def test_tmdb_variant_failure_keeps_good_results_and_other_sources(tmdb):
    engine, source = engine_for(tmdb, [movie()])

    async def variants(query):
        if query == "Interstellar":
            raise Problem("详情没有有效磁力", 502)
        return [resource()]

    source.search.side_effect = variants
    other = type(
        "Source",
        (),
        {
            "search": AsyncMock(
                return_value=[
                    {
                        **resource(suffix="b"),
                        "sources": [{"provider": "nyaa", "name": "Nyaa", "url": "https://nyaa.si/view/1"}],
                    }
                ]
            )
        },
    )()
    engine.sources["nyaa"] = other
    job = {"events": [], "results": [], "done": False}
    await engine.execute(SearchRequest(query="星际穿越", sources=["knaben", "nyaa"]), job)
    final = next(e for e in job["events"] if e.get("provider") == "knaben" and e["status"] == "done")
    assert len(final["results"]) == 1 and "详情" in final["error"]
    assert len(job["results"]) == 2 and other.search.await_count == 2


def test_tmdb_uses_tmdb_chinese_season_name_and_four_query_limit():
    show = movie(kind="tv", name="终结篇", english="The Finale", original="フィナーレ")
    show["seasons"] = [{"number": 2, "name": "最终季"}]
    assert media_queries("终结篇", show, 2) == ["终结篇 最终季", "终结篇 第二季", "终结篇 第2季", "The Finale S02"]
