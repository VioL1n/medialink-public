import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from medialink.app import create_app
from medialink.core import DEFAULTS, Problem, Store
from medialink.sources import Nyaa
from test_medialink import MAGNET, MemoryVault, authenticate
from test_tmdb import movie

EMPTY_PAGE = """<title>no match :: Nyaa</title>
<form action="/"><input name="q" value="no match"></form>
<div class="container"><h3>No results found</h3><div class="center">
<div class="pagination-page-info">Displaying results 0-0 out of 0 results.<br/>
Please refine your search results if you can't find what you were looking for.</div></div></div>"""
RESULT_PAGE = f'''<table class="torrent-list"><tbody><tr>
<td><a href="/?c=1_2">Anime</a></td><td><a href="/view/1">Interstellar 2014 1080p</a></td>
<td><a href="{MAGNET}">magnet</a></td><td>1.2 GiB</td><td data-timestamp="1700000000">Date</td>
<td>50</td><td>4</td><td>100</td></tr></tbody></table>'''
ASYNC_CLIENT = httpx.AsyncClient


def mock_transport(monkeypatch, handler):
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: ASYNC_CLIENT(**kwargs, transport=httpx.MockTransport(handler))
    )


@pytest.mark.parametrize(
    "body",
    [
        EMPTY_PAGE,
        EMPTY_PAGE.replace("<h3>No results found</h3>", "<h3><span>No results</span> found</h3>"),
        '<table class="torrent-list"><tbody></tbody></table>',
    ],
)
def test_nyaa_recognized_empty_results(body):
    assert Nyaa.parse(body, "https://nyaa.si") == []


@pytest.mark.parametrize(
    "body",
    [
        "",
        "<html></html>",
        "<h3>No results found</h3>",
        EMPTY_PAGE.replace('name="q"', 'name="unrelated"'),
        EMPTY_PAGE.replace("No results found", "Site unavailable"),
        EMPTY_PAGE.replace("Displaying results 0-0 out of 0 results.", "Unknown statistics"),
        EMPTY_PAGE.replace("0-0 out of 0", "0-1 out of 1"),
        '<script>const cssClass="torrent-list";</script>',
    ],
)
def test_nyaa_unknown_pages_are_not_successful_empty_results(body):
    with pytest.raises(Problem, match="无法识别") as error:
        Nyaa.parse(body, "https://nyaa.si")
    assert error.value.code == 502


def test_nyaa_result_structure_preserves_metadata_and_validation():
    found = Nyaa.parse(RESULT_PAGE, "https://nyaa.si")
    assert len(found) == 1
    assert found[0]["link"] == MAGNET and found[0]["size"] == "1.2 GiB"
    assert found[0]["resolution"] == "1080P" and found[0]["seeders"] == "50"
    assert found[0]["published"] and found[0]["sources"][0]["url"] == "https://nyaa.si/view/1"
    assert Nyaa.parse(RESULT_PAGE.replace(MAGNET, "magnet:?xt=invalid"), "https://nyaa.si") == []
    assert Nyaa.parse(RESULT_PAGE.replace("c=1_2", "c=2_1"), "https://nyaa.si") == []


async def test_nyaa_empty_search_is_cached_for_120_seconds(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=EMPTY_PAGE)

    mock_transport(monkeypatch, handler)
    source = Nyaa(lambda: DEFAULTS)
    before = time.time()
    assert await source.search("no match") == []
    assert await source.search("no match") == []
    assert len(calls) == 1
    key = ("no match", DEFAULTS["nyaa_url"], DEFAULTS["proxy"])
    expires, result = source.cache[key]
    assert before + 120 <= expires <= time.time() + 120 and result == []
    source.cache[key] = (0, [])
    source.last_request = 0
    assert await source.search("no match") == [] and len(calls) == 2


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(403, text=EMPTY_PAGE),
        httpx.Response(503, text="private-server-response"),
        httpx.ReadTimeout("private-transport-error"),
        httpx.ConnectError("private-transport-error"),
    ],
)
async def test_nyaa_http_and_network_errors_are_not_empty_results(monkeypatch, failure):
    def handler(request):
        if isinstance(failure, Exception):
            raise failure
        return failure

    mock_transport(monkeypatch, handler)
    source = Nyaa(lambda: DEFAULTS)
    with pytest.raises(Problem, match="暂时无法访问") as error:
        await source.search("no match")
    assert "private" not in str(error.value) and not source.cache


@pytest.mark.parametrize("body", ["", '<script>const marker="torrent-list";</script>'])
async def test_nyaa_unknown_responses_are_not_cached(monkeypatch, body):
    mock_transport(monkeypatch, lambda request: httpx.Response(200, text=body))
    source = Nyaa(lambda: DEFAULTS)
    with pytest.raises(Problem, match="无法识别"):
        await source.search("no match")
    assert not source.cache


@pytest.mark.parametrize("unknown", [False, True])
def test_nyaa_sse_empty_or_unknown_does_not_block_other_sources(tmp_path, monkeypatch, unknown):
    mock_transport(monkeypatch, lambda request: httpx.Response(200, text="" if unknown else EMPTY_PAGE))
    app = create_app(Store(tmp_path / "test.db"), MemoryVault(), start_workers=False)
    other = Nyaa.parse(RESULT_PAGE, "https://nyaa.si")
    app.state.knaben.search = AsyncMock(return_value=other)
    with TestClient(app) as client:
        authenticate(client)
        job = client.post("/api/v1/search", json={"query": "no match", "sources": ["nyaa", "knaben"]}).json()
        events = client.get("/api/v1/search/" + job["id"] + "/events").text
        messages = [json.loads(part.split("\n")[0]) for part in events.split("event: result\ndata: ")[1:]]
        final = next(message for message in reversed(messages) if message["provider"] == "nyaa")
        assert final["results"] == [] and final["status"] == ("error" if unknown else "done")
        assert ("error" in final) == unknown
        done = json.loads(events.split("event: done\ndata: ")[1].split("\n")[0])["results"]
        assert len(done) == 1 and done[0]["link"] == MAGNET


def test_nyaa_tmdb_chinese_empty_continues_to_english_results(tmp_path, monkeypatch):
    queries = []

    def handler(request):
        query = request.url.params["q"]
        queries.append(query)
        return httpx.Response(200, text=EMPTY_PAGE if query == "星际穿越" else RESULT_PAGE)

    mock_transport(monkeypatch, handler)
    vault = MemoryVault()
    vault.set("tmdb-credential", json.dumps({"type": "token", "value": "isolated-test-token"}))
    app = create_app(Store(tmp_path / "test.db"), vault, start_workers=False)
    selected = movie()
    app.state.tmdb.search = AsyncMock(return_value={"items": [selected], "updated": 1, "stale": False})
    app.state.tmdb.detail = AsyncMock(return_value=selected)
    with TestClient(app) as client:
        authenticate(client)
        job = client.post("/api/v1/search", json={"query": "星际穿越", "sources": ["nyaa"]}).json()
        events = client.get("/api/v1/search/" + job["id"] + "/events").text
        messages = [json.loads(part.split("\n")[0]) for part in events.split("event: result\ndata: ")[1:]]
        assert queries == ["星际穿越", "Interstellar"]
        assert messages[0]["status"] == "loading" and messages[0]["results"] == []
        assert messages[-1]["status"] == "done" and "error" not in messages[-1]
        assert messages[-1]["results"][0]["matched_queries"] == ["Interstellar"]
        assert "event: metadata" in events and "event: done" in events
