"""Daily anime schedule and episode subscriptions from Mikan Project (蜜柑计划)."""

import asyncio
import hashlib
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlparse
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup

from .core import Problem, blocking, resource_key
from .home import link_tmdb
from .naming import EXPLICIT_EPISODE, check_episode, episode_info, media_snapshot, season_numbers, tmdb_seasons
from .web_sources import Website

LABELS = {0: "周日", 1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "剧场版", 8: "OVA"}
SEASONS = ("冬", "春", "夏", "秋")
PAGE_MESSAGE = "蜜柑计划页面结构无法识别，站点可能已改版"
NETWORK_MESSAGE = "蜜柑计划暂时无法访问或请求超时，请检查代理"
SCHEDULE_TTL = 6 * 3600
CHECK_SECONDS = 30 * 60
TICK_SECONDS = 60
REQUEST_GAP = 3
MAX_BODY = 4 * 1024 * 1024
POSTER = re.compile(r"/images/Bangumi/(\d{6})/([A-Za-z0-9_-]+\.(?:jpg|png|webp))")
# Packs, batches and episode ranges are not single episodes.
BATCH = re.compile(r"合集|全集|batch|(?<![a-z])fin(?![a-z])|\d{1,4}\s*[-~～]\s*\d{1,4}\s*(?:[集话話]\s*)?(?:end\s*)?[\]】)）]",
                   re.I)
# Mikan shows release times in China Standard Time.
MIKAN_TIME = timezone(timedelta(hours=8))
SIZES = {"1920x1080": "1080P", "3840x2160": "2160P", "1280x720": "720P", "4k": "2160P"}
BGM_API = "https://api.bgm.tv/v0/subjects/"
BGM_MESSAGE = "Bangumi 番组计划暂时无法访问或请求超时，请检查代理"
BGM_TTL = 30 * 86400
# Bumped when linking learns new ways to match, so earlier cached misses are searched again.
LINK_VERSION = 2


def poster_url(value):
    match = POSTER.match(urlparse(value or "").path)
    return f"/api/v1/anime/images/Bangumi/{match[1]}/{match[2]}" if match else None


def release_episode(title):
    """Episode number of a single-episode release title, or None for packs and unclear titles."""
    if BATCH.search(title):
        return None
    _, episode, _, merged = episode_info(re.sub(r"[\\/]", " ", title) + ".mkv")
    return None if merged else episode


def release_resolution(title):
    match = re.search(r"(?<![a-z0-9])(2160p|1080[pi]|720p|480p|4k|1920x1080|3840x2160|1280x720)(?![a-z0-9])",
                      title, re.I)
    if not match:
        return None
    value = match[1].lower()
    return SIZES.get(value, value.upper())


def normalize_rules(rules):
    if rules is None:
        rules = {}
    if not isinstance(rules, dict):
        raise Problem("订阅规则格式无效", 422)
    wanted = rules.get("resolution") or None
    if wanted is not None and wanted not in {"2160P", "1080P", "720P"}:
        raise Problem("订阅清晰度只支持 2160P、1080P 或 720P", 422)
    words = {}
    for key in ("include", "exclude"):
        value = rules.get(key) or []
        if isinstance(value, str):
            value = re.split(r"[\s,，]+", value)
        if not isinstance(value, list) or any(not isinstance(word, str) for word in value):
            raise Problem("订阅关键词格式无效", 422)
        value = list(dict.fromkeys(word.strip()[:40] for word in value if word.strip()))
        if len(value) > 10:
            raise Problem("每类关键词最多 10 个", 422)
        words[key] = value
    return {"resolution": wanted, **words}


def matches(item, rules):
    title = item["title"].lower()
    if any(word.lower() not in title for word in rules["include"]):
        return False
    if any(word.lower() in title for word in rules["exclude"]):
        return False
    found = release_resolution(item["title"])
    return not (rules["resolution"] and found and found != rules["resolution"])


def pick(items, rules, number=release_episode):
    """Choose one release per episode: the wanted resolution first, then the newest."""
    chosen = {}
    for item in items:
        episode = number(item["title"])
        if episode is None or not matches(item, rules):
            continue
        rank = (release_resolution(item["title"]) == rules["resolution"], item.get("published") or "")
        if episode not in chosen or rank > chosen[episode][0]:
            chosen[episode] = (rank, item)
    return [(episode, chosen[episode][1]) for episode in sorted(chosen)]


def mikan_date(value):
    match = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", value or "")
    if not match:
        return None
    try:
        return datetime(int(match[1]), int(match[2]), int(match[3]), int(match[4] or 0), int(match[5] or 0),
                        tzinfo=MIKAN_TIME).isoformat()
    except ValueError:
        return None


def magnet(infohash, title):
    return f"magnet:?xt=urn:btih:{infohash.lower()}&dn={quote(title[:200])}"


class Mikan(Website):
    name = "蜜柑计划"
    setting = "mikan_url"
    network_message = NETWORK_MESSAGE

    def __init__(self, settings, data_dir):
        super().__init__(settings)
        self.image_dir = data_dir / "mikan-images"
        self.image_limit = 128 * 1024 * 1024
        self.image_lock = asyncio.Lock()
        # Posters are static files; a few may load at once without pacing.
        self.image_slots = asyncio.Semaphore(4)
        self.last_request = 0

    def base(self):
        return self.settings()[self.setting].rstrip("/")

    async def download(self, path, params):
        async with self.client() as client, client.stream("GET", self.base() + path, params=params) as response:
            response.raise_for_status()
            chunks, length = [], 0
            async for chunk in response.aiter_bytes():
                length += len(chunk)
                if length > MAX_BODY:
                    raise Problem("蜜柑计划返回的内容过大", 502)
                chunks.append(chunk)
            return b"".join(chunks)

    async def get(self, path, params=None):
        """Page and RSS requests are serialized and spaced out; the body size is capped."""
        try:
            async with asyncio.timeout(60), self.lock:
                await asyncio.sleep(max(0, self.last_request + REQUEST_GAP - time.time()))
                try:
                    return await self.download(path, params)
                finally:
                    self.last_request = time.time()
        except (httpx.HTTPError, TimeoutError):
            raise Problem(NETWORK_MESSAGE, 502) from None

    @staticmethod
    def parse_schedule(body, base):
        soup = BeautifulSoup(body, "html.parser")
        blocks = soup.select("div.sk-bangumi[data-dayofweek]")
        if not blocks:
            raise Problem(PAGE_MESSAGE, 502)
        days = []
        for block in blocks:
            try:
                weekday = int(block["data-dayofweek"])
            except ValueError:
                continue
            if weekday not in LABELS:
                continue
            items, seen = [], set()
            for row in block.select("li"):
                # Entries without a bangumi page link ("此番组下暂无作品") cannot be opened or subscribed.
                anchor = row.select_one("a.an-text[href]")
                link = urlparse(urljoin(base + "/", anchor["href"])) if anchor else None
                same_site = link and (link.scheme, link.netloc) == (urlparse(base).scheme, urlparse(base).netloc)
                found = re.fullmatch(r"/Home/Bangumi/(\d+)", link.path) if same_site and not link.query else None
                if not found or int(found[1]) in seen:
                    continue
                seen.add(int(found[1]))
                date = next((el.get_text(strip=True) for el in row.select(".date-text")
                             if "更新" in el.get_text()), "")
                poster = row.select_one("span[data-src]")
                items.append({"id": int(found[1]), "title": (anchor.get("title") or anchor.get_text()).strip()[:120]
                              or "未命名番剧", "poster": poster_url(poster["data-src"]) if poster else None,
                              "updated": (mikan_date(date) or "")[:10] or None})
            days.append({"weekday": weekday, "label": LABELS[weekday], "items": items})
        days.sort(key=lambda day: day["weekday"])
        header = soup.select_one(".date-select .date-text")
        current = re.search(r"(\d{4})\s*([冬春夏秋])", header.get_text(" ", strip=True)) if header else None
        seasons = []
        for link in soup.select("a[data-year][data-season]"):
            value = {"year": int(link["data-year"]), "season": link["data-season"]} if (
                link["data-year"].isdigit() and link["data-season"] in SEASONS) else None
            if value and value not in seasons:
                seasons.append(value)
        return {"year": int(current[1]) if current else None, "season": current[2] if current else None,
                "seasons": seasons[:24], "days": days}

    @staticmethod
    def parse_bangumi(body, bangumi_id):
        soup = BeautifulSoup(body, "html.parser")
        heading = soup.select_one(".bangumi-title")
        if not heading:
            raise Problem(PAGE_MESSAGE, 502)
        infos = [el.get_text(" ", strip=True) for el in soup.select(".bangumi-info")]
        weekday = next((key for info in infos if info.startswith("放送日期")
                        for key, label in LABELS.items() if label in info.replace("星期日", "周日").replace("星期", "周")),
                       None)
        start = next((re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", info) for info in infos
                      if info.startswith("放送开始")), None)
        bgm = soup.select_one('.bangumi-info a[href*="bgm.tv/subject/"], .bangumi-info a[href*="bangumi.tv/subject/"]')
        bgm_id = re.search(r"/subject/(\d+)", bgm["href"]) if bgm else None
        poster = soup.select_one(".bangumi-poster[style]")
        poster_match = re.search(r"url\(['\"]?([^'\")]+)", poster["style"]) if poster else None
        names = {}
        for link in soup.select("a.subgroup-name[data-anchor]"):
            anchor = link["data-anchor"].lstrip("#")
            if anchor.isdigit():
                names[int(anchor)] = link.get_text(strip=True)[:60]
        subgroups = []
        for block in soup.select(".subgroup-text[id]"):
            if not block["id"].isdigit():
                continue
            group = int(block["id"])
            first = block.select_one("a")
            releases = []
            table = block.find_next_sibling("div", class_="episode-table")
            for row in table.select("tbody tr") if table else []:
                link = row.select_one("a.magnet-link-wrap[href]")
                found = re.search(r"/Home/Episode/([a-fA-F0-9]{40})", link["href"]) if link else None
                cells = row.select("td")
                if not found or len(cells) < 4:
                    continue
                title = link.get_text(" ", strip=True)[:220]
                releases.append({"title": title, "hash": found[1].lower(), "magnet": magnet(found[1], title),
                                 "size": cells[2].get_text(strip=True) or None,
                                 "published": mikan_date(cells[3].get_text(strip=True)),
                                 "episode": release_episode(title), "resolution": release_resolution(title)})
                if len(releases) >= 8:
                    break
            subgroups.append({"id": group, "name": names.get(group) or (first.get_text(strip=True)[:60] if first
                                                                          else f"字幕组 {group}"),
                              "releases": releases})
        return {"id": bangumi_id, "title": heading.get_text(" ", strip=True)[:120] or "未命名番剧",
                "weekday": weekday,
                "start": f"{start[3]}-{int(start[1]):02d}-{int(start[2]):02d}" if start else None,
                "bgm_id": int(bgm_id[1]) if bgm_id else None,
                "poster": poster_url(poster_match[1]) if poster_match else None, "subgroups": subgroups}

    @staticmethod
    def parse_feed(body):
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            raise Problem("蜜柑计划 RSS 无法解析", 502) from None
        if root.find("channel") is None:
            raise Problem("蜜柑计划 RSS 无法解析", 502)
        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()[:220]
            page = (item.findtext("link") or "").strip()
            found = re.search(r"/Home/Episode/([a-fA-F0-9]{40})", page)
            if not title or not found:
                continue
            torrent = next((child for child in item if child.tag.endswith("}torrent")), None)
            values = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in torrent} if torrent is not None else {}
            enclosure = item.find("enclosure")
            length = values.get("contentLength") or (enclosure.get("length") if enclosure is not None else "")
            published = values.get("pubDate")
            try:
                published = datetime.fromisoformat(published).replace(tzinfo=MIKAN_TIME).isoformat() if published else None
            except ValueError:
                published = None
            items.append({"title": title, "hash": found[1].lower(), "link": magnet(found[1], title),
                          "page": page if page.startswith(("http://", "https://")) else None,
                          "size": int(length) if length and length.isdigit() else None, "published": published})
        return items

    async def schedule(self, year=None, season=None):
        base = self.base()
        if year is None:
            body = await self.get("/")
        else:
            body = await self.get("/Home/BangumiCoverFlowByDayOfWeek", {"year": year, "seasonStr": season})
        result = self.parse_schedule(body.decode("utf-8", "replace"), base)
        if year is not None:
            result.update(year=year, season=season)
        return result

    async def bangumi(self, bangumi_id):
        cached = self.cache.get(("bangumi", bangumi_id, self.base()))
        if cached and cached[0] > time.time():
            return cached[1]
        body = await self.get(f"/Home/Bangumi/{bangumi_id}")
        result = self.parse_bangumi(body.decode("utf-8", "replace"), bangumi_id)
        if len(self.cache) >= 100:
            self.cache.clear()
        self.cache[("bangumi", bangumi_id, self.base())] = (time.time() + 600, result)
        return result

    async def feed(self, bangumi_id, subgroup_id=None):
        params = {"bangumiId": bangumi_id}
        if subgroup_id is not None:
            params["subgroupid"] = subgroup_id
        body = await self.get("/RSS/Bangumi", params)
        return self.parse_feed(body)

    def read_image(self, file):
        if file.exists():
            os.utime(file, None)
            return file.read_bytes()
        return None

    def save_image(self, file, data):
        self.image_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = file.with_name(file.name + "." + secrets.token_hex(4) + ".tmp")
        try:
            with open(temporary, "xb") as output:
                os.chmod(temporary, 0o600)
                output.write(data)
            temporary.replace(file)
        finally:
            temporary.unlink(missing_ok=True)
        total = 0
        for cached in sorted(self.image_dir.glob("*.image"), key=lambda p: p.stat().st_mtime, reverse=True):
            total += cached.stat().st_size
            if total > self.image_limit:
                cached.unlink(missing_ok=True)

    @staticmethod
    def image_type(data):
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        return None

    async def image(self, path):
        match = POSTER.fullmatch("/images/" + path)
        if not match:
            raise Problem("图片路径无效", 422)
        file = self.image_dir / (hashlib.sha256((self.base() + "/" + path).encode()).hexdigest() + ".image")
        async with self.image_lock:
            data = await blocking(self.read_image, file)
        if not data:
            try:
                async with asyncio.timeout(30), self.image_slots:
                    data = await self.download("/images/" + path, {"width": 400, "height": 560, "format": "webp"})
            except (httpx.HTTPError, TimeoutError):
                raise Problem("蜜柑计划图片暂时无法加载", 502) from None
            if not self.image_type(data):
                raise Problem("蜜柑计划返回了无效的图片", 502)
            async with self.image_lock:
                await blocking(self.save_image, file, data)
        return data, self.image_type(data) or "application/octet-stream"


def bgm_names(data):
    """Original name first, then the Chinese name and aliases of a bgm.tv subject."""
    names = [data.get("name"), data.get("name_cn")]
    for info in data.get("infobox") if isinstance(data.get("infobox"), list) else []:
        if isinstance(info, dict) and info.get("key") in {"中文名", "别名"}:
            value = info.get("value")
            names += [v.get("v") for v in value if isinstance(v, dict)] if isinstance(value, list) else [value]
    return list(dict.fromkeys(name.strip()[:200] for name in names if isinstance(name, str) and name.strip()))[:12]


class BgmTv(Website):
    """bgm.tv subject names, cached in SQLite; Mikan pages link each show to its subject."""

    name = "Bangumi 番组计划"
    network_message = BGM_MESSAGE

    def __init__(self, settings, store):
        super().__init__(settings)
        self.store = store

    async def names(self, subject_id):
        key = f"bgm:subject:{subject_id}"
        cached = await blocking(self.store.cached_metadata, key)
        if cached and cached["expires"] > time.time():
            return cached["data"]["names"]
        try:
            async with asyncio.timeout(30), self.client() as client:
                response = await client.get(BGM_API + str(subject_id), headers={"Accept": "application/json"})
                # A removed subject has no names; it is cached like any other.
                data = {} if response.status_code == 404 else response.raise_for_status().json()
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise Problem(BGM_MESSAGE, 502) from None
        names = bgm_names(data if isinstance(data, dict) else {})
        await blocking(self.store.cache_metadata, key, {"names": names}, BGM_TTL)
        return names


def suggested_season(title):
    values = {value for value in season_numbers(title) if value > 0}
    return max(values) if values else 1


class AnimeSchedule:
    """Seasonal schedule cached in SQLite, with TMDB links resolved in the background."""

    def __init__(self, store, tmdb, mikan, bgm):
        self.store, self.tmdb, self.mikan, self.bgm = store, tmdb, mikan, bgm
        self.lock = asyncio.Lock()
        self.linking = None
        self.errors = {}

    def key(self, year=None, season=None):
        return "mikan:schedule:" + self.mikan.base() + (f":{year}:{season}" if year is not None else ":current")

    @staticmethod
    def items(data):
        for day in data["days"]:
            for item in day["items"]:
                yield day, item

    @staticmethod
    def candidate(day, item, year):
        # Theatrical releases are movies, OVAs may be either; the season year bounds TV matches.
        return {"id": str(item["id"]), "title": item["title"], "year": year,
                "kind": {7: "movie", 8: "anime"}.get(day["weekday"], "tv")}

    def names(self, bangumi_id, bgm_id=None):
        """Lazily look up the bgm.tv names of a show, found through its Mikan page unless already known."""
        async def lookup():
            subject = bgm_id or (await self.mikan.bangumi(bangumi_id))["bgm_id"]
            return await self.bgm.names(subject) if subject else []
        return lookup

    async def link_one(self, item, network, bgm_id=None):
        return await link_tmdb(self.store, self.tmdb, f"mikan:tmdb:{item['id']}", item, network,
                               self.names(int(item["id"]), bgm_id), LINK_VERSION)

    async def link(self, data, network):
        links = {}
        for day, item in self.items(data):
            if item["id"] not in links:
                links[item["id"]] = await self.link_one(self.candidate(day, item, data.get("year")), network)
            item["media"] = links[item["id"]]
        return data

    async def link_all(self, key, data):
        try:
            data = await self.link(data, network=True)
        except Problem:
            # TMDB is optional; keep links found so far.
            data = await self.link(data, network=False)
        await blocking(self.store.cache_metadata, key, data, SCHEDULE_TTL)

    async def schedule(self, year=None, season=None, force=False):
        if (year is None) != (season is None) or (year is not None and (season not in SEASONS
                                                                        or not 2000 <= year <= 2100)):
            raise Problem("番剧季度无效", 422)
        key = self.key(year, season)
        async with self.lock:
            cached = await blocking(self.store.cached_metadata, key)
            if cached and not force and cached["expires"] > time.time():
                return {**cached["data"], "updated": cached["updated"], "stale": False, "error": None}
            try:
                data = await self.mikan.schedule(year, season)
            except Problem as exc:
                if cached:
                    return {**cached["data"], "updated": cached["updated"], "stale": True, "error": exc.message}
                raise
            if year is not None and not data["seasons"]:
                current = await blocking(self.store.cached_metadata, self.key())
                data["seasons"] = current["data"]["seasons"] if current else []
            data = await self.link(data, network=False)
            updated = await blocking(self.store.cache_metadata, key, data, SCHEDULE_TTL)
        if self.linking and not self.linking.done():
            self.linking.cancel()
        self.linking = asyncio.create_task(self.link_all(key, data))
        return {**data, "updated": updated, "stale": False, "error": None}

    async def suggestion(self, detail):
        year = int(detail["start"][:4]) if detail.get("start") else None
        kind = {7: "movie", 8: "anime"}.get(detail.get("weekday"), "tv")
        item = {"id": str(detail["id"]), "title": detail["title"], "year": year, "kind": kind}
        try:
            media = await self.link_one(item, True, detail.get("bgm_id"))
        except Problem:
            media = await self.link_one(item, False)
        if media and media["type"] == "tv":
            media = {**media, "season_number": suggested_season(detail["title"])}
        return media

    async def close(self):
        if self.linking and not self.linking.done():
            self.linking.cancel()
            await asyncio.gather(self.linking, return_exceptions=True)


class Subscriptions:
    """Poll each subscription's RSS feed and submit one ingest task per new episode."""

    def __init__(self, store, mikan, tmdb, submit):
        self.store, self.mikan, self.tmdb, self.submit = store, mikan, tmdb, submit
        self.lock = asyncio.Lock()
        self.stopped = False
        self.wake = asyncio.Event()

    async def numbering(self, subscription):
        """Episode numbers within the subscribed season; groups mix absolute (171) and per-season (01) numbers."""
        season, seasons = subscription["season_number"], None
        if subscription["media_type"] == "tv" and season:
            try:
                seasons = tmdb_seasons(media_snapshot(await self.tmdb.detail("tv", subscription["media_id"])))
            except Problem:
                pass

        def number(title):
            episode = release_episode(title)
            mapped, _, error = check_episode(seasons, season, episode, bool(EXPLICIT_EPISODE.search(title)))
            return episode if error else mapped

        return number

    async def check(self, subscription_id):
        async with self.lock:
            subscription = await blocking(self.store.subscription, subscription_id)
            now = time.time()
            try:
                items = await self.mikan.feed(subscription["bangumi_id"], subscription["subgroup_id"])
                claimed = {e["episode"] for e in await blocking(self.store.subscription_episodes, subscription_id)}
                number = await self.numbering(subscription)
                for episode, item in pick(items, subscription["rules"], number):
                    if episode in claimed:
                        continue
                    task_id = None
                    if subscription["initialized"] or subscription["backfill"]:
                        task = await self.submit({
                            "link": item["link"], "title": item["title"], "target": subscription["target"],
                            "auto_naming": True, "media": {"type": subscription["media_type"],
                                                           "id": subscription["media_id"],
                                                           "season_number": subscription["season_number"]},
                            "sources": [{"provider": "mikan", "name": "蜜柑计划", "url": item["page"]
                                         or self.mikan.base() + f"/Home/Bangumi/{subscription['bangumi_id']}"}],
                        }, {"subscription": {"id": subscription_id, "episode": episode}})
                        task_id = task["id"]
                    # Claimed after submitting: a crash in between resubmits, and task creation is idempotent.
                    await blocking(self.store.claim_episode, subscription_id, episode,
                                   resource_key(item["link"]), item["title"], task_id)
            except Problem as exc:
                failures = subscription["failures"] + 1
                return await blocking(self.store.subscription, subscription_id, checked=now, error=exc.message,
                                      failures=failures, next_check=now + min(6 * 3600, 900 * 2 ** min(failures, 5)))
            return await blocking(self.store.subscription, subscription_id, checked=now, error=None, failures=0,
                                  initialized=1, next_check=now + CHECK_SECONDS)

    async def run(self):
        while not self.stopped:
            for subscription in await blocking(self.store.subscriptions):
                if self.stopped:
                    return
                if subscription["enabled"] and subscription["next_check"] <= time.time():
                    try:
                        await self.check(subscription["id"])
                    except Problem:
                        pass
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), TICK_SECONDS)
            except TimeoutError:
                pass
