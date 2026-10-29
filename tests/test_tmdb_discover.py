import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from medialink.app import create_app
from medialink.core import Problem, Store
from medialink.tmdb import TMDB
from test_medialink import MemoryVault, authenticate
from test_tmdb import raw_movie, transport


@pytest.fixture
def tmdb(tmp_path):
    vault = MemoryVault()
    vault.set("tmdb-credential", json.dumps({"type": "token", "value": "discover-test-token"}))
    return TMDB(Store(tmp_path / "db.sqlite"), vault)


def configure(tmdb, response=None):
    calls = []

    def handler(request):
        calls.append(request)
        path = request.url.path
        if "/genre/" in path:
            genres = [{"id": 18, "name": "剧情"}, {"id": 35, "name": "喜剧"}]
            genres += [{"id": 878, "name": "科幻"}] if "/movie/" in path else [{"id": 10765, "name": "科幻与奇幻"}]
            return httpx.Response(200, json={"genres": genres})
        if path.endswith("/countries"):
            return httpx.Response(200, json=[{"iso_3166_1": "CN", "english_name": "China"},
                                           {"iso_3166_1": "US", "native_name": "美国"}])
        if path.endswith("/languages"):
            return httpx.Response(200, json=[{"iso_639_1": "en", "english_name": "English"},
                                           {"iso_639_1": "cn", "english_name": "Cantonese"}])
        if response:
            return response(request)
        raw = raw_movie()
        if path.endswith("/tv"):
            raw.update(name="剧集", first_air_date="2014-01-01")
        return httpx.Response(200, json={"results": [raw], "total_pages": 3})

    transport(tmdb, handler)
    return calls


async def test_filters_array_config_persistence_and_labels(tmdb):
    calls = configure(tmdb)
    result = await tmdb.filters()
    assert result["countries"][0] == {"id": "CN", "name": "中国大陆"}
    assert result["languages"][1] == {"id": "cn", "name": "粤语"}
    assert len(calls) == 3
    assert await TMDB(Store(tmdb.store.path), tmdb.vault).filters() == result
    assert len(calls) == 3
    with tmdb.store.connect() as db:
        for row in db.execute("SELECT key,data,updated,expires FROM metadata_cache"):
            assert 86399 <= row[3] - row[2] <= 86401
            assert "discover-test-token" not in row[0] + row[1]


@pytest.mark.parametrize("kind,date_key", [("movie", "primary_release_date"), ("tv", "first_air_date")])
async def test_discover_all_conditions_and_pagination(tmdb, kind, date_key):
    calls = configure(tmdb)
    result = await tmdb.discover(kind, "35,18,18", "878" if kind == "movie" else "10765",
                                 2000, 2009, "US", "cn", 2)
    params = dict(calls[-1].url.params)
    assert calls[-1].url.path == f"/3/discover/{kind}"
    assert params == {"language": "zh-CN", "include_adult": "false", "sort_by": "popularity.desc",
                      "page": "2", "with_genres": "18,35",
                      "without_genres": "878" if kind == "movie" else "10765",
                      date_key + ".gte": "2000-01-01", date_key + ".lte": "2009-12-31",
                      "with_origin_country": "US", "with_original_language": "cn"}
    assert result["items"][0]["type"] == kind
    assert result["page"] == 2 and result["total_pages"] == 3


@pytest.mark.parametrize("args", [
    {"media_type": "all"}, {"include_genres": "18|35"}, {"include_genres": "18", "exclude_genres": "18"},
    {"include_genres": "-1"}, {"include_genres": "999"}, {"media_type": "tv", "include_genres": "878"},
    {"year_from": 0}, {"year_to": 10000}, {"year_from": 2026, "year_to": 2025},
    {"country": "XX"}, {"original_language": "xx"}, {"page": 501},
])
async def test_invalid_conditions_rejected(tmdb, args):
    calls = configure(tmdb)
    with pytest.raises(Problem) as error:
        await tmdb.discover(**args)
    assert error.value.code == 422
    assert not any("/discover/" in str(r.url) for r in calls)


async def test_cache_canonicalization_isolation_and_expiry(tmdb):
    calls = configure(tmdb)
    original = await tmdb.discover(include_genres="35,18", year_from=1000, year_to=1959)
    assert await tmdb.discover(include_genres="18,35,18", year_from=1000, year_to=1959) == original
    await tmdb.discover(include_genres="18,35", year_from=1000, year_to=1959, page=2)
    await tmdb.discover(include_genres="18,35", year_from=1000, year_to=1959, country="CN")
    await tmdb.discover(include_genres="18,35", year_from=1000, year_to=1959, original_language="en")
    await tmdb.discover(include_genres="18,35", year_from=1000, year_to=1960)
    await tmdb.discover(exclude_genres="18,35")
    await tmdb.discover(media_type="tv", include_genres="18,35")
    assert len([r for r in calls if "/discover/" in str(r.url)]) == 7
    assert dict(calls[3].url.params)["primary_release_date.gte"] == "1000-01-01"
    with tmdb.store.connect() as db:
        rows = db.execute("SELECT updated,expires FROM metadata_cache WHERE key LIKE 'tmdb:/discover/%'").fetchall()
        assert all(1799 <= r[1] - r[0] <= 1801 for r in rows)


async def test_zero_results_are_cached(tmdb):
    calls = configure(tmdb, lambda r: httpx.Response(200, json={"results": [], "total_pages": 0}))
    result = await tmdb.discover()
    assert result["items"] == [] and result["total_pages"] == 0 and not result["stale"]
    assert await tmdb.discover() == result and len(calls) == 4


@pytest.mark.parametrize("response", [httpx.Response(503), httpx.Response(200, json={}),
                                      httpx.Response(200, json=[]), httpx.Response(200, json={"results": "bad", "total_pages": 1})])
async def test_faults_not_reported_as_empty(tmdb, response):
    configure(tmdb, lambda r: response)
    with pytest.raises(Problem) as error:
        await tmdb.discover()
    assert error.value.code == 502


async def test_discover_rate_limit_old_cache(tmdb):
    configure(tmdb)
    original = await tmdb.discover()
    with tmdb.store.connect() as db:
        db.execute("UPDATE metadata_cache SET expires=0 WHERE key LIKE 'tmdb:/discover/%'")
    configure(tmdb, lambda r: httpx.Response(429, headers={"Retry-After": "30"}))
    result = await tmdb.discover()
    assert result["items"] == original["items"] and result["updated"] == original["updated"] and result["stale"]
    assert tmdb.wait_until > time.time() + 28
    with pytest.raises(Problem) as error:
        await tmdb.discover(page=2)
    assert error.value.code == 429


async def test_array_response_only_allowed_for_configuration(tmdb):
    transport(tmdb, lambda r: httpx.Response(200, json=[]))
    assert (await tmdb.fetch("/configuration/languages"))["data"] == []
    with pytest.raises(Problem):
        await tmdb.fetch("/genre/movie/list")


def test_discover_routes_authentication_validation_and_existing_tasks(tmdb):
    app = create_app(tmdb.store, tmdb.vault, start_workers=False)
    configure(app.state.tmdb)
    with TestClient(app) as client:
        assert client.get("/api/v1/tmdb/filters").status_code == 401
        assert client.get("/api/v1/tmdb/discover").status_code == 401
        authenticate(client)
        assert client.get("/api/v1/tmdb/filters?media_type=tv").status_code == 200
        result = client.get("/api/v1/tmdb/discover?include_genres=18&year_from=2014&year_to=2014").json()
        assert result["items"][0]["id"] == 100
        assert client.get("/api/v1/tmdb/discover?year_to=10000").status_code == 422
        assert client.get("/api/v1/tmdb/discover?include_genres=999").status_code == 422
        assert client.get("/api/v1/tasks").status_code == 200
