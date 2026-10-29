from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from medialink.anime import MIKAN_TIME
from medialink.core import Problem, Store
from medialink.local_home import LocalHome, parse_post, telegram_entries
from medialink.tmdb import TMDB
from medialink.web_sources import Eztv, Yts
from test_medialink import MemoryVault

YEAR = datetime.now(MIKAN_TIME).year
# Fixed hours of one China Standard Time day, so same-day ordering does not depend on when the test runs.
NOON = datetime.now(MIKAN_TIME).replace(hour=12, minute=0, second=0, microsecond=0)


def at(hours=0.0, days=0):
    return NOON - timedelta(hours=hours, days=days)


def share(key):
    return {"key": "share:115:" + key, "kind": "share"}


def release(char, item, hours, days=0, **extra):
    return {"hash": char * 40, "item": item, "published": at(hours, days).timestamp(), **extra}


def detail(title, year=YEAR, genres=(18,), popularity=50, votes=100, rating=7.5, countries=("US",), language="en",
           last=None, names=("剧情",)):
    return {"title": title, "chinese_title": title, "english_title": None, "original_title": title, "year": year,
            "rating": rating, "votes": votes, "poster": "/p.jpg", "backdrop": "/b.jpg", "overview": "简介",
            "genres": list(names), "genre_ids": list(genres), "countries": list(countries),
            "original_language": language, "popularity": popularity, "last_air_date": last}


class FakeTMDB:
    def __init__(self, titles, imdb):
        self.titles, self.imdb, self.found, self.searched = titles, imdb, [], []

    async def find_imdb(self, imdb_id):
        self.found.append(imdb_id)
        return self.imdb.get(imdb_id)

    async def search(self, query):
        self.searched.append(query)
        return {"items": [{"type": kind, "id": id_, "title": value["title"], "year": value["year"]}
                          for (kind, id_), value in self.titles.items()]}

    async def detail(self, media_type, media_id):
        if (media_type, media_id) not in self.titles:
            raise Problem("TMDB 影片或资料不存在", 404)
        return {"type": media_type, "id": media_id, **self.titles[(media_type, media_id)]}


def test_parse_post_reads_channel_formats():
    def parsed(body, *keys):
        result = parse_post(body)
        return tuple(result[key] for key in keys) if result else None

    keys = ("title", "year", "kind", "episode", "total", "quality", "dv", "cam")
    assert parsed("🎬 空枪 (2026) 杜比高码\n\n🌟 评分： 6.3", *keys) == ("空枪", 2026, "movie", None, None, None, False,
                                                                     False)
    assert parsed("📺 嫁金钗 (2026) S01E16 ✨4K WEB-DL AAC", *keys) == ("嫁金钗", 2026, "tv", (1, 16), None, "4K", False,
                                                                      False)
    assert parsed("🎬 醉梦 (2026) 全18集", "kind", "total") == ("tv", 18)
    assert parsed("🎬 赛博朋克：边缘行者 (2022) 第二季", "kind", "episode") == ("tv", (2, 0))
    assert parsed("📺 此处通往繁星 (2026) 第1季 更新至第7集 ✨4K WEB-DL", "episode") == ((1, 7),)
    assert parsed("📺 幸福伽菜子的快乐杀手生活 (2025) 共2季 第2季更新至第5集 1080P", "episode", "quality") == ((2, 5), "1080P")
    assert parsed("📺 电视剧｜行尸走肉：达里尔·迪克森 (2023) 第3季 全7集", "title", "episode", "total") == (
        "行尸走肉：达里尔·迪克森", (3, 0), 7)
    assert parsed("🎬 空枪 (2026) 空枪 (2026)[杜比视界+SDR]{tmdbid-1567937}", "title", "tmdb_id", "dv") == (
        "空枪", 1567937, True)
    assert parsed("🎬 某片 (2026) HDTC 枪版", "cam") == (True,)
    assert parsed("🎬 某片 (2026) 1080p DTS-HD MA", "cam", "quality") == (False, "1080P")
    assert parse_post("Telegram必备的搜索引擎，极搜JISOU帮你精准找到") is None
    assert parse_post("#互推") is None and parse_post("") is None


def test_telegram_entries_group_posts_and_count_each_share_once():
    since = at(days=14).timestamp()
    messages = [
        {"body": f"📺 嫁金钗 ({YEAR}) S01E16 4K", "published": at(1).isoformat(), "resources": [share("a")]},
        {"body": f"🎬 嫁金钗 ({YEAR}) 4K", "published": at(2).isoformat(), "resources": [share("a"), share("b")]},
        {"body": f"📺 嫁金钗 ({YEAR}) S01E01", "published": at(days=20).isoformat(), "resources": [share("c")]},
        {"body": f"🎬 枪版片 ({YEAR}) HDTS", "published": at(1).isoformat(), "resources": [share("d")]},
        {"body": f"🎬 无资源 ({YEAR})", "published": at(1).isoformat(), "resources": []},
    ]
    entries = telegram_entries(messages, since)
    assert set(entries) == {f"tg:嫁金钗:{YEAR}", f"tg:枪版片:{YEAR}"}
    drama = entries[f"tg:嫁金钗:{YEAR}"]
    assert drama["kind"] == "tv" and drama["episode"] == (1, 16) and drama["quality"] == "4K"
    assert drama["shares"] == {"share:115:a", "share:115:b"} and drama["time"] == at(1).timestamp()
    assert drama["curated"] and drama["real"] and not entries[f"tg:枪版片:{YEAR}"]["real"]


def test_yts_and_eztv_rows_keep_valid_torrents_only():
    yts = Yts.parse({"status": "ok", "data": {"movies": [
        {"imdb_code": "tt1234567", "title": "Prison Heart", "year": YEAR, "rating": 6.8, "torrents": [
            {"hash": "A" * 40, "quality": "2160p", "date_uploaded_unix": 1792742668},
            {"hash": "bad", "quality": "1080p", "date_uploaded_unix": 1792742668}, "junk"]},
        {"imdb_code": "nm123", "torrents": [{"hash": "B" * 40, "date_uploaded_unix": 1}]},
        {"imdb_code": "tt7654321", "title": "Unrated", "year": YEAR, "rating": 0, "torrents": [
            {"hash": "C" * 40, "quality": "720p", "date_uploaded_unix": 1792742000}]}]}})
    assert [(r["hash"], r["item"], r["quality"], r["rating"]) for r in yts] == [
        ("a" * 40, "imdb:tt1234567", "2160P", 6.8), ("c" * 40, "imdb:tt7654321", "720P", None)]
    assert Yts.parse({"status": "ok", "data": {"movie_count": 0}}) == []
    with pytest.raises(Problem):
        Yts.parse({"status": "error"})
    eztv = Eztv.parse({"torrents_count": 3, "torrents": [
        {"hash": "D" * 40, "imdb_id": "7235466", "season": "10", "episode": "2", "date_released_unix": 1792744699,
         "title": "9-1-1 S10E02 1080p WEB H264-SuccessfulCrab EZTV"},
        {"hash": "E" * 40, "imdb_id": "0", "date_released_unix": 1792744699},
        {"hash": "F" * 40, "imdb_id": "0413573", "season": "x", "episode": "", "date_released_unix": 1792744000,
         "filename": "Show.S01E01.XviD.avi"}]})
    assert [(r["item"], r["season"], r["episode"], r["quality"]) for r in eztv] == [
        ("imdb:tt7235466", 10, 2, "1080P"), ("imdb:tt0413573", None, None, None)]
    with pytest.raises(Problem):
        Eztv.parse({"torrents": "nope"})


async def test_find_imdb_prefers_titles_then_episode_shows():
    tmdb = TMDB(MagicMock(), MemoryVault())
    tmdb.fetch = AsyncMock(return_value={"data": {"movie_results": [], "tv_results": [{"id": 5}]}})
    assert await tmdb.find_imdb("tt2531336") == {"type": "tv", "id": 5}
    assert tmdb.fetch.await_args.args == ("/find/tt2531336", {"external_source": "imdb_id"}, 3 * 86400)
    tmdb.fetch.return_value = {"data": {"movie_results": [{"id": 9, "adult": True}],
                                        "tv_episode_results": [{"id": 1, "show_id": 7}]}}
    assert await tmdb.find_imdb("tt2531337") == {"type": "tv", "id": 7}
    tmdb.fetch.return_value = {"data": {}}
    assert await tmdb.find_imdb("tt2531338") is None
    with pytest.raises(Problem):
        await tmdb.find_imdb("../movie/1")


def test_record_releases_keeps_one_row_per_hash_inside_the_window(tmp_path):
    store = Store(tmp_path / "state.db")
    since = at(days=14).timestamp()
    store.record_releases([release("a", "imdb:tt1", 1), release("a", "imdb:tt1", 2), release("b", "imdb:tt1", 0,
                                                                                            days=30)], since)
    assert [row["hash"] for row in store.recent_releases(since)] == ["a" * 40]
    store.record_releases([], at(0).timestamp())
    assert store.recent_releases(since) == []


@pytest.fixture
def local(tmp_path):
    store = Store(tmp_path / "state.db")
    with store.connect() as db:
        db.execute("INSERT INTO channels(id,peer,title,mode) VALUES ('web:demo','demo','115 频道','public_web')")
        db.execute("INSERT INTO channels(id,peer,title,mode,enabled) VALUES ('web:off','off','停用','public_web',0)")
    posts = [
        (f"📺 嫁金钗 ({YEAR}) S01E16 ✨4K WEB-DL", [share("a")], at(2)),
        (f"📺 嫁金钗 ({YEAR}) S01E15 ✨4K WEB-DL", [share("b")], at(3)),
        (f"🎬 空枪 ({YEAR}) 杜比视界 {{tmdbid-11}}", [share("c")], at(1)),
        (f"🎬 火遮眼 ({YEAR}) {{tmdbid-999}}", [share("d")], at(4)),
        ("🎬 武林外传 (2006) 4K", [share("e")], at(1)),
        (f"🎬 枪版片 ({YEAR}) HDTC", [share("f")], at(1)),
        ("🎬 赛博朋克：边缘行者 (2022) 第二季", [share("g")], at(5)),
        ("Telegram必备的搜索引擎，极搜JISOU帮你精准找到", [share("h")], at(1)),
        (f"📺 旧剧 ({YEAR}) S01E01", [share("i")], at(days=20)),
    ]
    for number, (body, resources, moment) in enumerate(posts, 1):
        store.put_message("web:demo", number, body, resources, moment.isoformat())
    store.put_message("web:off", 1, f"🎬 停用频道 ({YEAR})", [share("j")], at(1).isoformat())
    titles = {
        ("movie", 10): detail("监狱雄心", popularity=78),
        ("movie", 11): detail("空枪", popularity=4, votes=7, countries=("CN",), names=("剧情", "犯罪")),
        ("movie", 12): detail("冷门片", popularity=5),
        ("movie", 14): detail("烂片", rating=5.0, votes=50),
        ("movie", 15): detail("老片", year=2010),
        ("movie", 16): detail("火遮眼", popularity=31, countries=("CN",)),
        ("movie", 17): detail("武林外传", year=2006),
        ("tv", 20): detail("紧急呼救", year=2018, popularity=147, votes=2876, last=f"{YEAR}-10-20"),
        ("tv", 21): detail("嫁金钗", popularity=107, votes=4, countries=("CN",), last=f"{YEAR}-10-23"),
        ("tv", 23): detail("真人秀", genres=(10764,), popularity=60, last=f"{YEAR}-10-22"),
        ("tv", 30): detail("药屋少女的呢喃", year=2023, genres=(16,), popularity=128, countries=("JP",),
                           language="ja", last=f"{YEAR}-10-23", names=("动画", "Sci-Fi & Fantasy")),
        ("tv", 31): detail("无人问津", genres=(16,), countries=("JP",), language="ja", last=f"{YEAR}-10-23"),
        ("tv", 40): detail("赛博朋克：边缘行者", year=2022, genres=(16,), popularity=40, countries=("JP",),
                           language="ja", last=f"{YEAR}-09-30"),
    }
    imdb = {"tt100": {"type": "movie", "id": 10}, "tt102": {"type": "movie", "id": 12},
            "tt104": {"type": "movie", "id": 14}, "tt105": {"type": "movie", "id": 15},
            "tt200": {"type": "tv", "id": 20}, "tt201": {"type": "tv", "id": 21}, "tt203": {"type": "tv", "id": 23}}
    tmdb = FakeTMDB(titles, imdb)
    yts, eztv = MagicMock(), MagicMock()
    yts.latest = AsyncMock(return_value=[
        release("a", "imdb:tt100", 0.5, kind="movie", quality="2160P", rating=6.8, title="Prison Heart"),
        release("b", "imdb:tt101", 0.5, kind="movie", rating=4.0),
        release("c", "imdb:tt102", 0.5, kind="movie", rating=7.0),
        release("d", "imdb:tt104", 0.5, kind="movie", rating=6.5),
        release("e", "imdb:tt105", 0.5, kind="movie", rating=7.0)])
    eztv.latest = AsyncMock(return_value=[
        release("f", "imdb:tt200", 0.2, kind="tv", season=10, episode=2, quality="1080P"),
        release("g", "imdb:tt200", 0.3, kind="tv", season=10, episode=2, quality="720P"),
        release("h", "imdb:tt201", 6, kind="tv", season=1, episode=17),
        release("i", "imdb:tt201", 6.5, kind="tv", season=1, episode=17),
        release("j", "imdb:tt202", 0.1, kind="tv", season=1, episode=1),
        release("k", "imdb:tt203", 0.2, kind="tv", season=3, episode=1),
        release("l", "imdb:tt203", 0.3, kind="tv", season=3, episode=1)])
    anime = MagicMock()
    anime.schedule = AsyncMock(return_value={"days": [{"weekday": 1, "items": [
        {"id": 3001, "title": "药屋少女的呢喃 第三季", "updated": NOON.date().isoformat(), "media": {"type": "tv", "id": 30}},
        {"id": 3002, "title": "无人问津", "updated": NOON.date().isoformat(), "media": {"type": "tv", "id": 31}},
        {"id": 3003, "title": "未关联", "updated": NOON.date().isoformat(), "media": None}]}]})
    anime.mikan.base.return_value = "https://mikanani.me"

    async def feed(bangumi_id):
        return [{"title": "[组] 药屋少女的呢喃 - 51 [1080p]", "hash": "m" * 40, "published": at(7).isoformat()},
                {"title": "[组] 药屋少女的呢喃 - 03 [1080p]", "hash": "n" * 40,
                 "published": at(days=30).isoformat()}] if bangumi_id == 3001 else []

    anime.mikan.feed = AsyncMock(side_effect=feed)
    return LocalHome(store, tmdb, yts, eztv, anime)


async def test_local_home_merges_filters_and_orders_recent_resources(local):
    result = await local.home()
    sections = {section["key"]: section["items"] for section in result["sections"]}
    assert result["errors"] == [] and result["origin"] == "local"
    # Within one day titles with 115 shares lead; then the newest resource first.
    assert [item["title"] for item in sections["movie"]] == ["空枪", "火遮眼", "监狱雄心"]
    assert [item["title"] for item in sections["tv"]] == ["嫁金钗", "紧急呼救"]
    assert [item["title"] for item in sections["anime"]] == ["赛博朋克：边缘行者", "药屋少女的呢喃"]
    drama = sections["tv"][0]
    assert drama["counts"] == {"magnet": 2, "cloud": 2, "online": 0} and drama["status"] == "更新至第17集"
    assert drama["media"] == {"type": "tv", "id": 21} and drama["qualities"] == ["4K"]
    assert drama["ratings"] == {"tmdb": 7.5} and drama["heat"] is None and drama["tags"] == ["大陆", "剧情"]
    movie = sections["movie"][2]
    assert movie["ratings"] == {"tmdb": 7.5, "imdb": 6.8} and movie["qualities"] == ["4K"]
    assert sections["movie"][0]["qualities"] == ["DV"] and sections["tv"][1]["status"] == "第10季第2集"
    assert sections["tv"][1]["heat"] == "2876" and sections["anime"][0]["status"] == "第2季"
    assert sections["anime"][1]["tags"] == ["日本", "动画", "科幻"] and sections["anime"][1]["counts"]["magnet"] == 1
    assert [item["title"] for item in result["featured"]] == ["监狱雄心", "火遮眼", "空枪"]
    # Cheap checks skip low-rated YTS films, one-off EZTV uploads and old reposts before any lookup.
    assert "tt101" not in local.tmdb.found and "tt202" not in local.tmdb.found
    assert "武林外传" not in local.tmdb.searched and "空枪" not in local.tmdb.searched
    assert "火遮眼" in local.tmdb.searched

    # Later passes reuse recorded torrents and fresh Mikan feeds when sites fail or return nothing new.
    local.yts.latest = AsyncMock(side_effect=Problem("YTS 暂时无法访问或请求超时，请检查代理", 502))
    local.eztv.latest = AsyncMock(return_value=[])
    feeds = local.anime.mikan.feed.await_count
    again = await local.home()
    assert again["errors"] == ["YTS 暂时无法访问或请求超时，请检查代理"]
    assert {item["title"] for section in again["sections"] for item in section["items"]} == {
        "空枪", "火遮眼", "监狱雄心", "嫁金钗", "紧急呼救", "赛博朋克：边缘行者", "药屋少女的呢喃"}
    assert local.anime.mikan.feed.await_count == feeds


async def test_local_home_reports_source_errors_only_without_titles(tmp_path):
    store = Store(tmp_path / "state.db")
    failing = MagicMock()
    failing.latest = AsyncMock(side_effect=Problem("EZTV 暂时无法访问或请求超时，请检查代理", 502))
    anime = MagicMock()
    anime.schedule = AsyncMock(side_effect=Problem("蜜柑计划暂时无法访问或请求超时，请检查代理", 502))
    home = LocalHome(store, FakeTMDB({}, {}), failing, failing, anime)
    with pytest.raises(Problem) as caught:
        await home.home()
    assert caught.value.message == "EZTV 暂时无法访问或请求超时，请检查代理；蜜柑计划暂时无法访问或请求超时，请检查代理"
    anime.schedule = AsyncMock(return_value={"days": []})
    failing.latest = AsyncMock(return_value=[])
    assert (await home.home())["sections"] == []


async def test_tmdb_credential_errors_stop_the_build(local):
    local.tmdb.find_imdb = AsyncMock(side_effect=Problem("请先在连接设置中配置 TMDB 凭据", 503))
    with pytest.raises(Problem) as caught:
        await local.home()
    assert caught.value.code == 503
