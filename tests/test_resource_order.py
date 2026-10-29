import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from medialink.app import SearchRequest, create_app
from medialink.core import Store
from medialink.search_jobs import SearchEngine, rank_results
from test_medialink import MemoryVault, authenticate
from test_tmdb import movie, resource


def item(quality, suffix="a", title="Interstellar 2014", score=None):
    result = {**resource(title, suffix), "resolution": quality}
    if score is not None:
        result["match"] = {"score": score, "status": "review", "explanation": "test"}
    return result


@pytest.mark.parametrize("media", [None, movie()])
def test_all_quality_levels_and_unknown_values(media):
    qualities = [None, "invalid", "480P", "720P", "1080I", "1080P", "2160P", "4K", "8K"]
    inputs = [item(q, chr(97 + i)) for i, q in enumerate(qualities)]
    result = rank_results(inputs, media)
    assert [r["resolution"] for r in result] == ["8K", "2160P", "4K", "1080P", "1080I", "720P", "480P", None, "invalid"]
    assert [r["resolution"] for r in inputs] == qualities
    assert all("match" not in r for r in inputs)


def test_same_quality_match_score_stability_and_case():
    inputs = [item(" 2160p ", "a", score=60), item("4k", "b", score=90), item("2160P", "c", score=60),
              item("4K", "d", score=-40), item(None, "e", score=90), item("1080P", "f", score=0)]
    assert [r["key"] for r in rank_results(inputs)] == [inputs[i]["key"] for i in [1, 0, 2, 3, 5, 4]]


def test_review_4k_precedes_matched_1080p_but_retains_warning():
    low = item("1080P", "a")
    wrong_year = item("2160P", "b", "Interstellar 1999")
    other_4k = item("4K", "c")
    result = rank_results([low, wrong_year, other_4k], movie())
    assert [r["key"] for r in result] == [other_4k["key"], wrong_year["key"], low["key"]]
    assert result[1]["match"]["status"] == "review" and "年份不同" in result[1]["match"]["explanation"]
    assert result[2]["match"]["status"] == "matched"


def test_same_quality_season_match_and_resource_kinds():
    inputs = [item("4K", "a", "Stranger Things S01 2016"),
              {**item("2160P", "b", "Stranger Things S02 2016"), "kind": "share"},
              item("1080P", "c", "Stranger Things S02 2016")]
    result = rank_results(inputs, movie(name="怪奇物语", english="Stranger Things", kind="tv", year=2016), 2)
    assert [r["key"] for r in result] == [inputs[i]["key"] for i in [1, 0, 2]]
    assert "季数不同" in result[1]["match"]["explanation"]


@pytest.mark.parametrize("assisted", [False, True])
async def test_late_high_quality_batch_and_final_results(assisted):
    low, high, highest = item("1080P", "a"), item("2160P", "b", "Interstellar 1999"), item("8K", "c")
    release_high, release_highest = asyncio.Event(), asyncio.Event()

    async def low_source(query):
        return [low]

    async def high_source(query):
        await release_high.wait()
        return [low, high]

    async def highest_source(query):
        await release_highest.wait()
        return [highest]

    tmdb = AsyncMock()
    engine = SearchEngine({"low": AsyncMock(search=low_source), "high": AsyncMock(search=high_source),
                           "highest": AsyncMock(search=highest_source)}, tmdb)
    if assisted:
        engine.resolve = AsyncMock(return_value={"media": movie(), "queries": ["Interstellar"], "season_number": None})
    request = SearchRequest(query="Interstellar", sources=["low", "high", "highest"], use_tmdb=assisted)
    job = {"events": [], "results": [], "done": False}
    running = asyncio.create_task(engine.execute(request, job))
    async with asyncio.timeout(2):
        while not job["results"]:
            await asyncio.sleep(0)
        assert job["results"][0]["key"] == low["key"]
        release_high.set()
        while len(job["results"]) < 2:
            await asyncio.sleep(0)
        assert [r["key"] for r in job["results"]] == [high["key"], low["key"]]
        high_batch = next(e for e in job["events"] if e.get("provider") == "high")
        assert high_batch["results"][0]["key"] == high["key"]
        release_highest.set()
        await running
    assert job["done"] and [r["key"] for r in job["results"]] == [highest["key"], high["key"], low["key"]]


@pytest.mark.parametrize("assisted", [False, True])
def test_http_sse_done_order_and_filtered_order(tmp_path, assisted):
    vault = MemoryVault()
    vault.set("tmdb-credential", json.dumps({"type": "token", "value": "isolated-order-test-token"}))
    app = create_app(Store(tmp_path / "db.sqlite"), vault, start_workers=False)
    app.state.tmdb.detail = AsyncMock(return_value=movie())
    app.state.knaben.search = AsyncMock(return_value=[item("1080P", "a"), item("4K", "b", "Interstellar 1999")])
    app.state.nyaa.search = AsyncMock(return_value=[
        {**item("8K", "c"), "sources": [{"provider": "nyaa", "name": "Nyaa", "url": "https://example.org/nyaa"}]},
        {**item("2160P", "d"), "sources": [{"provider": "nyaa", "name": "Nyaa", "url": "https://example.org/nyaa"}]},
    ])
    with TestClient(app) as client:
        authenticate(client)
        body = {"query": "Interstellar", "sources": ["knaben", "nyaa"], "use_tmdb": assisted}
        if assisted:
            body["media"] = {"type": "movie", "id": 100}
        job = client.post("/api/v1/search", json=body).json()
        stream = client.get("/api/v1/search/" + job["id"] + "/events").text
        result = json.loads(stream.split("event: done\ndata: ")[1].split("\n")[0])["results"]
        assert [r["resolution"] for r in result] == (["8K", "2160P", "4K", "1080P"] if assisted else ["8K", "4K", "2160P", "1080P"])
        assert [r["resolution"] for r in result if any(s["provider"] == "knaben" for s in r["sources"])] == ["4K", "1080P"]
