import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from medialink.anime import (
    AnimeSchedule,
    BgmTv,
    Mikan,
    Subscriptions,
    bgm_names,
    matches,
    normalize_rules,
    pick,
    release_episode,
    release_resolution,
    suggested_season,
)
from medialink.core import DEFAULTS, Problem, Store
from test_medialink import authenticate, client, store  # noqa: F401

BASE = "https://mikanani.me"
FIXTURES = Path(__file__).parent / "fixtures"
HOME = (FIXTURES / "mikan_home.html").read_text()
BANGUMI = (FIXTURES / "mikan_bangumi.html").read_text()
RSS = (FIXTURES / "mikan_rss.xml").read_bytes()
SUBJECT = json.loads((FIXTURES / "bgm_subject.json").read_text())
ORIGINAL = "貸した魔力は【リボ払い】で強制徴収"
TV = {"id": 260001, "type": "tv", "title": "用循环利息强制收回借出的魔力", "chinese_title": "用循环利息强制收回借出的魔力",
      "original_title": "貸した魔力は【リボ払い】で強制徴収", "year": 2026, "seasons": [{"number": 1, "episodes": 12}]}


def release(title, published="2026-10-11T00:00:00+08:00", infohash="a" * 40):
    return {"title": title, "hash": infohash, "link": f"magnet:?xt=urn:btih:{infohash}", "page": None,
            "size": 1, "published": published}


def test_parse_schedule_days_posters_and_seasons():
    result = Mikan.parse_schedule(HOME, BASE)
    assert (result["year"], result["season"]) == (2026, "秋")
    assert result["seasons"][:2] == [{"year": 2026, "season": "秋"}, {"year": 2026, "season": "夏"}]
    days = {day["weekday"]: day for day in result["days"]}
    assert [day["weekday"] for day in result["days"]] == [0, 6, 7, 8]
    assert (days[7]["label"], days[8]["label"]) == ("剧场版", "OVA")
    saturday = days[6]["items"]
    assert saturday[1] == {"id": 4060, "title": "用【循环利息】强制收回借出的魔力",
                           "poster": "/api/v1/anime/images/Bangumi/202610/f132283b.jpg", "updated": "2026-10-16"}
    # Entries without works have no bangumi link and are left out.
    assert [item["id"] for item in days[7]["items"]] == [4122, 4124, 4127]


def test_parse_schedule_rejects_offsite_links_and_unknown_pages():
    offsite = HOME.replace('href="/Home/Bangumi/4060"', 'href="https://evil.example/Home/Bangumi/4060"')
    ids = [item["id"] for day in Mikan.parse_schedule(offsite, BASE)["days"] for item in day["items"]]
    assert 4060 not in ids
    with pytest.raises(Problem):
        Mikan.parse_schedule("<main>改版了</main>", BASE)


def test_parse_bangumi_groups_and_links():
    result = Mikan.parse_bangumi(BANGUMI, 4060)
    assert (result["title"], result["weekday"], result["start"], result["bgm_id"]) == (
        "用【循环利息】强制收回借出的魔力", 6, "2026-10-03", 606716)
    assert [(g["id"], g["name"]) for g in result["subgroups"]] == [
        (615, "Kirara Fantasia"), (1256, "Nix-Raws"), (583, "ANi"), (370, "LoliHouse")]
    ani = result["subgroups"][2]["releases"]
    assert [(r["episode"], r["resolution"], r["size"]) for r in ani] == [(2, "1080P", "385.4 MB"), (1, "1080P", "435.5 MB")]
    assert ani[0]["magnet"].startswith("magnet:?xt=urn:btih:085a592e80f42d2e08bff1f0624f952dea14be33")
    assert ani[0]["published"] == "2026-10-11T02:01:00+08:00"


def test_parse_feed_magnets_sizes_and_dates():
    items = Mikan.parse_feed(RSS)
    assert len(items) == 13
    first = items[0]
    assert first["hash"] == "ef3c231cef58561214d7e14c2d74eb3514197d22"
    assert first["link"].startswith("magnet:?xt=urn:btih:ef3c231cef58561214d7e14c2d74eb3514197d22&dn=")
    assert first["size"] == 719532864 and first["published"].startswith("2026-10-16T01:00:34")
    broken = RSS.replace(b"/Home/Episode/ef3c231cef58561214d7e14c2d74eb3514197d22</link><title>",
                         b"/Home/Episode/not-a-hash</link><title>")
    assert len(Mikan.parse_feed(broken)) == 12
    with pytest.raises(Problem):
        Mikan.parse_feed(b"<html>not rss</html>")


@pytest.mark.parametrize(("title", "episode"), [
    ("[黒ネズミたち] 用【循环利息】强制收回借出的魔力 / Revo - 02 (ABEMA 1920x1080 AVC AAC MKV)", 2),
    ("[Nix-Raws] 用【循环利息】 / Kashita Maryoku S01E02 [CR WEB-DL 1080p AVC AAC][简繁内封]", 2),
    ("[LoliHouse] 某番 - 05v2 [WebRip 1080p HEVC-10bit AAC]", 5),
    ("[字幕组] 某番 [12][1080P][简体]", 12),
    ("【喵萌奶茶屋】某番 第3话 1080p", 3),
    ("[ANi] 86 - 02 [1080P][Baha][WEB-DL]", 2),
    ("[黒ネズミたち] 黑色五叶草 Season 2 / Black Clover 2nd Season - 172 (ABEMA 1920x1080 AVC AAC MKV)", 172),
    ("[字幕组] 某番 [01-12][1080P][合集]", None),
    ("[字幕组] 某番 [01-12 精校][1080p]", None),
    ("[字幕组] 某番 01-12 Fin [1080p]", None),
    ("[字幕组] 某番 [1080P]", None),
])
def test_release_episode(title, episode):
    assert release_episode(title) == episode


def test_rules_filter_and_pick_prefers_resolution_then_newest():
    rules = normalize_rules({"resolution": "1080P", "include": "简", "exclude": ["HEVC"]})
    assert rules == {"resolution": "1080P", "include": ["简"], "exclude": ["HEVC"]}
    assert release_resolution("Revo - 02 (CR 1920x1080 AVC)") == "1080P"
    assert not matches(release("某番 - 01 [720p][简体]"), rules)
    assert not matches(release("某番 - 01 [1080p][简体][HEVC]"), rules)
    assert matches(release("某番 - 01 [简体]"), rules)
    items = [release("某番 - 01 [简体]", "2026-10-12T00:00:00+08:00", "b" * 40),
             release("某番 - 01 [1080p][简体]", "2026-10-11T00:00:00+08:00", "c" * 40),
             release("某番 - 02 [1080p][简体]", "2026-10-18T00:00:00+08:00", "d" * 40),
             release("某番 - 02 [1080p][简体]", "2026-10-19T00:00:00+08:00", "e" * 40),
             release("某番 [01-02][1080p][简体]", "2026-10-20T00:00:00+08:00", "f" * 40)]
    assert [(episode, item["hash"][0]) for episode, item in pick(items, rules)] == [(1, "c"), (2, "e")]
    for bad in ({"resolution": "4K"}, {"include": [1]}, {"exclude": [str(i) for i in range(11)]}, []):
        with pytest.raises(Problem):
            normalize_rules(bad)


def test_season_labels_before_episode_numbers():
    from medialink.naming import episode_info, season_numbers
    assert season_numbers("Show Season - 12") == set() and season_numbers("Show Season-2") == {2}
    assert season_numbers("Show 3rd Season") == {3} and season_numbers("Show Season.03") == {3}
    assert episode_info("Black Clover 2nd Season - 172 [1080p].mkv")[:3] == (2, 172, None)


def test_suggested_season():
    assert suggested_season("某番 第二季") == 2 and suggested_season("某番 Season 3") == 3
    assert suggested_season("某番") == 1


class FakeMikan:
    def __init__(self, items):
        self.items, self.calls = items, []

    def base(self):
        return BASE

    async def feed(self, bangumi_id, subgroup_id=None):
        self.calls.append((bangumi_id, subgroup_id))
        if isinstance(self.items, Exception):
            raise self.items
        return self.items


class FakeTMDB:
    def __init__(self, seasons=None):
        self.seasons = seasons

    async def detail(self, media_type, media_id):
        if self.seasons is None:
            raise Problem("请先在连接设置中配置 TMDB 凭据", 503)
        return {**TV, "seasons": [{"number": n, "episodes": c} for n, c in self.seasons.items()]}


def subscription_values(**extra):
    return {"bangumi_id": 4060, "subgroup_id": 583, "subgroup_name": "ANi", "title": "用【循环利息】强制收回借出的魔力",
            "poster": None, "media_type": "tv", "media_id": 260001, "season_number": 1, "target": "/115/infuse/动漫",
            "rules": normalize_rules({"resolution": "1080P"}), "backfill": True, **extra}


@pytest.fixture
def local_store(tmp_path):
    return Store(tmp_path / "state.db")


async def test_subscription_submits_each_episode_once(local_store):
    mikan = FakeMikan([release("[ANi] 某番 - 01 [1080P]", infohash="1" * 40),
                       release("[ANi] 某番 - 02 [1080P]", infohash="2" * 40)])
    submitted = []

    async def submit(payload, extra):
        submitted.append((payload, extra))
        return {"id": f"task-{extra['subscription']['episode']}"}

    subscriptions = Subscriptions(local_store, mikan, FakeTMDB(), submit)
    subscription = local_store.create_subscription(subscription_values())
    result = await subscriptions.check(subscription["id"])
    assert mikan.calls == [(4060, 583)]
    assert [extra["subscription"]["episode"] for _, extra in submitted] == [1, 2]
    payload = submitted[0][0]
    assert payload["auto_naming"] is True and payload["target"] == "/115/infuse/动漫"
    assert payload["media"] == {"type": "tv", "id": 260001, "season_number": 1}
    assert payload["sources"][0]["provider"] == "mikan"
    assert result["initialized"] == 1 and result["error"] is None and result["next_check"] > time.time()
    assert [(e["episode"], e["task_id"]) for e in local_store.subscription_episodes(subscription["id"])] == [
        (1, "task-1"), (2, "task-2")]
    await subscriptions.check(subscription["id"])
    assert len(submitted) == 2
    mikan.items = mikan.items + [release("[ANi] 某番 - 03 [1080P]", infohash="3" * 40)]
    await subscriptions.check(subscription["id"])
    assert [extra["subscription"]["episode"] for _, extra in submitted] == [1, 2, 3]


async def test_subscription_without_backfill_skips_aired_episodes(local_store):
    mikan = FakeMikan([release("[ANi] 某番 - 01 [1080P]", infohash="1" * 40)])
    submit = AsyncMock(return_value={"id": "task"})
    subscriptions = Subscriptions(local_store, mikan, FakeTMDB(), submit)
    subscription = local_store.create_subscription(subscription_values(backfill=False))
    await subscriptions.check(subscription["id"])
    submit.assert_not_awaited()
    assert [(e["episode"], e["task_id"]) for e in local_store.subscription_episodes(subscription["id"])] == [(1, None)]
    mikan.items = mikan.items + [release("[ANi] 某番 - 02 [1080P]", infohash="2" * 40)]
    await subscriptions.check(subscription["id"])
    assert submit.await_count == 1 and submit.await_args.args[1]["subscription"]["episode"] == 2


async def test_subscription_failure_records_error_and_backs_off(local_store):
    mikan = FakeMikan(Problem("蜜柑计划暂时无法访问", 502))
    subscriptions = Subscriptions(local_store, mikan, FakeTMDB(), AsyncMock())
    subscription = local_store.create_subscription(subscription_values())
    result = await subscriptions.check(subscription["id"])
    assert result["error"] == "蜜柑计划暂时无法访问" and result["failures"] == 1
    assert result["next_check"] >= time.time() + 1700 and result["initialized"] == 0
    mikan.items = [release("[ANi] 某番 - 01 [1080P]")]
    submit = AsyncMock(side_effect=Problem("目录不存在", 404))
    subscriptions.submit = submit
    result = await subscriptions.check(subscription["id"])
    assert result["error"] == "目录不存在" and result["failures"] == 2
    assert local_store.subscription_episodes(subscription["id"]) == []


async def test_subscription_maps_absolute_numbers_to_the_season(local_store):
    mikan = FakeMikan([release("[黒ネズミたち] 黑色五叶草 2nd Season - 01 (B-Global 1920x1080)", infohash="1" * 40),
                       release("[黒ネズミたち] 黑色五叶草 2nd Season - 171 (ABEMA 1920x1080)", infohash="2" * 40),
                       release("[黒ネズミたち] 黑色五叶草 2nd Season - 172 (ABEMA 1920x1080)", infohash="3" * 40)])
    submit = AsyncMock(side_effect=lambda payload, extra: {"id": "task-" + str(extra["subscription"]["episode"])})
    subscriptions = Subscriptions(local_store, mikan, FakeTMDB({1: 170, 2: 12}), submit)
    subscription = local_store.create_subscription(subscription_values(season_number=2))
    await subscriptions.check(subscription["id"])
    assert [(e["episode"], e["task_id"]) for e in local_store.subscription_episodes(subscription["id"])] == [
        (1, "task-1"), (2, "task-2")]
    # Without TMDB the raw numbers are used.
    subscriptions.tmdb = FakeTMDB()
    assert (await subscriptions.numbering(local_store.subscription(subscription["id"])))(
        "黑色五叶草 2nd Season - 171") == 171


def test_store_rejects_duplicate_subscription(local_store):
    local_store.create_subscription(subscription_values())
    with pytest.raises(Problem):
        local_store.create_subscription(subscription_values())
    other = local_store.create_subscription(subscription_values(subgroup_id=None))
    local_store.delete_subscription(other["id"])
    with pytest.raises(Problem):
        local_store.subscription(other["id"])


async def test_schedule_links_tmdb_in_background(local_store):
    mikan = Mikan(lambda: DEFAULTS, local_store.path.parent)
    mikan.schedule = AsyncMock(return_value=Mikan.parse_schedule(HOME, BASE))
    tmdb = AsyncMock()
    tmdb.search.return_value = {"items": [{"type": "tv", "id": 260001, "title": "用【循环利息】强制收回借出的魔力",
                                           "year": 2026}]}
    schedule = AnimeSchedule(local_store, tmdb, mikan, AsyncMock())
    data = await schedule.schedule()
    assert all(item["media"] is None for day in data["days"] for item in day["items"])
    await schedule.linking
    data = await schedule.schedule()
    linked = {item["id"]: item["media"] for day in data["days"] for item in day["items"]}
    assert linked[4060] == {"type": "tv", "id": 260001}
    mikan.schedule.assert_awaited_once()
    mikan.schedule.side_effect = Problem("蜜柑计划暂时无法访问", 502)
    stale = await schedule.schedule(force=True)
    assert stale["stale"] and stale["error"] == "蜜柑计划暂时无法访问" and stale["days"]
    with pytest.raises(Problem):
        await schedule.schedule(2026, "盛夏")


def test_subscription_api_creates_tasks_and_lists_episodes(client):  # noqa: F811
    assert client.get("/api/v1/subscriptions").status_code == 401
    authenticate(client)
    state = client.app.state
    state.tmdb.detail = AsyncMock(return_value=TV)
    state.mikan.bangumi = AsyncMock(return_value=Mikan.parse_bangumi(BANGUMI, 4060))
    state.mikan.feed = AsyncMock(return_value=Mikan.parse_feed(RSS))
    body = {"bangumi_id": 4060, "subgroup_id": 583, "media": {"type": "tv", "id": 260001, "season_number": 1},
            "target": "/115/infuse/动漫", "rules": {"resolution": "1080P"}}
    assert client.post("/api/v1/subscriptions", json={**body, "subgroup_id": 999}).status_code == 422
    assert client.post("/api/v1/subscriptions", json={**body, "target": "/elsewhere"}).status_code == 422
    assert client.post("/api/v1/subscriptions", json={**body, "media": {"type": "tv", "id": 260001}}).status_code == 422
    response = client.post("/api/v1/subscriptions", json=body)
    assert response.status_code == 200, response.text
    subscription = response.json()
    assert subscription["subgroup_name"] == "ANi" and subscription["error"] is None
    assert [e["episode"] for e in subscription["episodes"]] == [1, 2]
    assert all(e["state"] == "queued" for e in subscription["episodes"])
    tasks = client.get("/api/v1/tasks").json()
    assert sorted(t["subscription"]["episode"] for t in tasks) == [1, 2]
    task = client.get(f"/api/v1/tasks/{tasks[0]['id']}").json()
    assert task["naming"]["media"]["id"] == 260001
    assert client.post("/api/v1/subscriptions", json=body).status_code == 409
    again = client.post(f"/api/v1/subscriptions/{subscription['id']}/check").json()
    assert len(again["episodes"]) == 2 and len(client.get("/api/v1/tasks").json()) == 2
    paused = client.patch(f"/api/v1/subscriptions/{subscription['id']}",
                          json={"enabled": False, "rules": {"resolution": "720P", "exclude": "HEVC"}}).json()
    assert paused["enabled"] == 0 and paused["rules"] == {"resolution": "720P", "include": [], "exclude": ["HEVC"]}
    assert client.patch(f"/api/v1/subscriptions/{subscription['id']}", json={"rules": {"resolution": "4K"}}).status_code == 422
    assert client.delete(f"/api/v1/subscriptions/{subscription['id']}").status_code == 204
    assert client.get("/api/v1/subscriptions").json() == []
    assert len(client.get("/api/v1/tasks").json()) == 2


def test_anime_routes_validate_inputs_and_settings(client):  # noqa: F811
    authenticate(client)
    assert client.get("/api/v1/anime/images/Bangumi/202610/../../secret.jpg").status_code in {404, 422}
    assert client.get("/api/v1/anime/images/other/202610/a.jpg").status_code == 422
    assert client.get("/api/v1/anime/schedule?year=2026&season=盛夏").status_code == 422
    assert client.get("/api/v1/anime/bangumi/0").status_code == 422
    assert client.patch("/api/v1/settings", json={"mikan_url": "ftp://mikan"}).status_code == 422
    assert client.patch("/api/v1/settings", json={"anime_target": "/elsewhere"}).status_code == 422
    saved = client.patch("/api/v1/settings", json={"mikan_url": "https://mikanime.tv",
                                                   "anime_target": "/115/infuse/动漫/"}).json()
    assert saved["mikan_url"] == "https://mikanime.tv" and saved["anime_target"] == "/115/infuse/动漫"
    assert client.patch("/api/v1/settings", json={"anime_target": ""}).json()["anime_target"] == ""


async def test_bgm_names_are_fetched_once_and_cached(local_store):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/1"):
            return httpx.Response(404, json={"title": "Not Found"})
        if request.url.path.endswith("/2"):
            return httpx.Response(503)
        return httpx.Response(200, json=SUBJECT)

    bgm = BgmTv(lambda: DEFAULTS, local_store)
    bgm.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    names = await bgm.names(606716)
    assert names == bgm_names(SUBJECT) and names[:3] == [ORIGINAL, "用【循环利息】强制收回借出的魔力",
                                                          "借出去的魔力要用【分期还款】强制收回"]
    assert len(names) == len(set(names)) == 5
    assert await bgm.names(606716) == names and await bgm.names(1) == [] and await bgm.names(1) == []
    with pytest.raises(Problem):
        await bgm.names(2)
    assert calls == ["/v0/subjects/606716", "/v0/subjects/1", "/v0/subjects/2"]
    assert bgm_names({"name": " ", "infobox": "x"}) == []


async def test_schedule_links_by_bgm_original_name_and_retries_old_misses(local_store):
    mikan = Mikan(lambda: DEFAULTS, local_store.path.parent)
    mikan.schedule = AsyncMock(return_value=Mikan.parse_schedule(HOME, BASE))
    page = Mikan.parse_bangumi(BANGUMI, 4060)
    mikan.bangumi = AsyncMock(side_effect=lambda bangumi_id: page if bangumi_id == 4060 else {**page, "bgm_id": None})
    bgm = AsyncMock()
    bgm.names.return_value = bgm_names(SUBJECT)
    queries = []

    async def search(query):
        queries.append(query)
        # TMDB names the show differently in Chinese; only its original title is shared with bgm.tv.
        return {"items": [{"type": "tv", "id": 333622, "title": "借出去的魔力要用分期还款强制收回",
                           "original_title": ORIGINAL, "year": 2026}] if query == ORIGINAL else []}

    tmdb = AsyncMock()
    tmdb.search.side_effect = search
    # A miss cached before bgm.tv names were used must not keep the show unlinked.
    local_store.cache_metadata("mikan:tmdb:4060", {"media": None}, 86400)
    schedule = AnimeSchedule(local_store, tmdb, mikan, bgm)
    await schedule.schedule()
    await schedule.linking
    data = await schedule.schedule()
    linked = {item["id"]: item["media"] for day in data["days"] for item in day["items"]}
    assert linked[4060] == {"type": "tv", "id": 333622}
    assert [media for key, media in linked.items() if key != 4060] == [None] * (len(linked) - 1)
    assert queries[queries.index("用【循环利息】强制收回借出的魔力") + 1] == ORIGINAL and queries.count(ORIGINAL) == 1
    bgm.names.assert_awaited_once_with(606716)
    assert local_store.cached_metadata("mikan:tmdb:4060")["data"]["version"] == 2
    # New misses are not searched again; a failed bgm.tv lookup is retried on the next pass.
    other = next(key for key in linked if key != 4060)
    assert local_store.cached_metadata(f"mikan:tmdb:{other}")["data"] == {"media": None, "version": 2}
    mikan.bangumi.side_effect = Problem("蜜柑计划暂时无法访问", 502)
    local_store.cache_metadata(f"mikan:tmdb:{other}", {"media": None}, 86400)
    queries.clear()
    item = {"id": str(other), "title": "某番", "year": 2026, "kind": "tv"}
    assert await schedule.link_one(item, True) is None
    assert queries == ["某番"] and local_store.cached_metadata(f"mikan:tmdb:{other}")["data"]["version"] == 1
    assert await schedule.link_one(item, True) is None and queries == ["某番", "某番"]


async def test_suggestion_uses_the_bgm_id_from_the_page(local_store):
    tmdb = AsyncMock()
    tmdb.search.side_effect = lambda query: {"items": [{"type": "tv", "id": 333622, "title": "借出去的魔力",
                                                        "original_title": ORIGINAL, "year": 2026}]
                                             if query == ORIGINAL else []}
    mikan, bgm = AsyncMock(), AsyncMock()
    bgm.names.return_value = [ORIGINAL]
    schedule = AnimeSchedule(local_store, tmdb, mikan, bgm)
    detail = {**Mikan.parse_bangumi(BANGUMI, 4060), "title": "用【循环利息】强制收回借出的魔力 第二季"}
    assert await schedule.suggestion(detail) == {"type": "tv", "id": 333622, "season_number": 2}
    mikan.bangumi.assert_not_awaited()
    bgm.names.assert_awaited_once_with(606716)
