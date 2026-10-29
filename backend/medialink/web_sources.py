import asyncio
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .core import Problem, blocking, resource_key
from .sources import extract_resources, merge_results, resolution


def http_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
        return value if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username else None
    except ValueError:
        return None


def published_date(value):
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc).isoformat()
        if value:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    return None


def byte_size(value):
    if not isinstance(value, (int, float)) or value < 0:
        return None
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024


class Website:
    name = "网站"
    setting = ""
    network_message = None

    def __init__(self, settings):
        self.settings = settings
        self.cache = {}
        self.lock = asyncio.Lock()

    def client(self):
        return httpx.AsyncClient(timeout=18, proxy=self.settings()["proxy"] or None, trust_env=False,
                                 follow_redirects=True, headers={"User-Agent": "MediaLink/0.2"})

    async def search(self, query):
        settings = self.settings()
        key = (query, settings[self.setting], settings["proxy"])
        try:
            async with asyncio.timeout(60), self.lock:
                cached = self.cache.get(key)
                if cached and cached[0] > time.time():
                    return cached[1]
                async with self.client() as client:
                    results = await self.fetch(client, query, settings[self.setting].rstrip("/"))
                if len(self.cache) >= 100:
                    self.cache.clear()
                self.cache[key] = (time.time() + 120, results)
                return results
        except (httpx.HTTPError, TimeoutError):
            raise Problem(self.network_message or f"{self.name} 暂时无法访问或请求超时，请检查代理", 502) from None


class Knaben(Website):
    name = "Knaben"
    setting = "knaben_url"

    @staticmethod
    def parse(data):
        if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
            raise Problem("Knaben 返回了无法识别的数据", 502)
        results = []
        for hit in data["hits"][:50]:
            if not isinstance(hit, dict):
                continue
            tracker = str(hit.get("tracker") or "")
            if "1337x" in tracker.lower() or "1337x" in str(hit.get("trackerId", "")).lower():
                continue
            category = str(hit.get("category") or "")
            categories = hit.get("categoryId") or []
            if not isinstance(categories, list):
                categories = [categories]
            if not (category.lower().startswith(("movies", "tv")) or
                    any(str(c).startswith(("200", "300")) for c in categories)):
                continue
            link = hit.get("magnetUrl")
            if not isinstance(link, str) or not link.lower().startswith("magnet:?"):
                continue
            try:
                key = resource_key(link)
            except Problem:
                continue
            title = str(hit.get("title") or "未命名资源")[:220]
            seeders = hit.get("seeders")
            results.append({"key": key, "title": title, "kind": "magnet", "link": link, "password": "",
                            "size": byte_size(hit.get("bytes")), "resolution": resolution(title),
                            "published": published_date(hit.get("date")),
                            "seeders": str(seeders) if isinstance(seeders, (int, float)) and seeders >= 0 else None,
                            "sources": [{"provider": "knaben", "name": "Knaben" + (" · " + tracker if tracker else ""),
                                         "url": http_url(hit.get("details")) or "https://knaben.org"}]})
        return merge_results(results)

    async def fetch(self, client, query, base):
        response = await client.get(base + "/v2/search", params={"q": query, "c": "2000000,3000000",
                                                               "s": 50, "o": "seeders", "d": "desc"})
        response.raise_for_status()
        try:
            return self.parse(response.json())
        except ValueError:
            raise Problem("Knaben 返回了无法识别的数据", 502) from None


class LimeTorrents(Website):
    name = "LimeTorrents"
    setting = "limetorrents_url"
    challenge_message = "站点要求人机验证，可暂时取消勾选此来源，其他来源仍可搜索"
    network_message = "网络访问失败或请求超时，请检查代理或稍后重试"
    page_message = "页面结构无法识别，站点可能已改版"
    magnet_message = "详情没有有效磁力"

    @classmethod
    def page(cls, body, headers=None):
        soup = BeautifulSoup(body, "html.parser")
        heading = re.compile(r"(?:verify (?:that )?you are human|just a moment|checking your browser)[.!…\s]*", re.I)
        if ((headers or {}).get("cf-mitigated", "").lower() == "challenge" or
                soup.select_one('#challenge-form, form[action*="/cdn-cgi/challenge-platform/"]') or
                any(heading.fullmatch(el.get_text(" ", strip=True)) for el in soup.select("title,h1,h2"))):
            raise Problem(cls.challenge_message, 502)
        return soup

    @classmethod
    def check_response(cls, response):
        # Check before HTTP status: challenges may return either 200 or 403.
        cls.page(response.text, response.headers)
        response.raise_for_status()

    @classmethod
    def parse_search(cls, body, base):
        soup = cls.page(body)
        if not soup.select_one(".table2"):
            if (soup.select_one('form#searchform input[name="q"]') and
                    soup.select_one("#content .querytext") and
                    any(el.get_text(" ", strip=True).lower() == "no results found"
                        for el in soup.select("#content h2"))):
                return []
            raise Problem(cls.page_message, 502)
        items, seen = [], set()
        for row in soup.select(".table2 tr"):
            # The first anchor can be a /leet2/ advertisement, never a torrent detail.
            anchor = row.select_one('div.tt-name > a[href*="-torrent-"][href$=".html"]')
            cells = row.select("td")
            if not anchor or len(cells) < 2:
                continue
            detail = urljoin(base + "/", anchor["href"])
            if (urlparse(detail).netloc != urlparse(base).netloc or detail in seen or
                    not re.fullmatch(r"/[^/?]+-torrent-\d+\.html", urlparse(detail).path)):
                continue
            category = cells[1].get_text(" ", strip=True)
            if not re.search(r"\bin (?:Movies|TV shows|Anime)\b", category, re.I):
                continue
            seen.add(detail)
            title = anchor.get_text(" ", strip=True) or "未命名资源"
            date = cells[1].select_one("[title]")
            seeds = row.select_one(".tdseed")
            items.append({"title": title[:220], "detail": detail,
                          "size": cells[2].get_text(strip=True) or None if len(cells) > 2 else None,
                          "published": published_date(date.get("title")) if date else None,
                          "seeders": seeds.get_text(strip=True) or None if seeds else None})
        return items[:20]

    @classmethod
    def parse_detail(cls, body, item):
        soup = cls.page(body)
        results = extract_resources(item["title"], [a["href"] for a in soup.select('a[href^="magnet:"]')],
                                    {"provider": "limetorrents", "name": "LimeTorrents", "url": item["detail"]},
                                    item["published"])
        if not results:
            raise Problem(cls.magnet_message if soup.select_one(".torrentinfo") else cls.page_message, 502)
        return [{**r, "size": item["size"], "seeders": item["seeders"]} for r in results]

    async def fetch(self, client, query, base):
        response = await client.get(base + "/search/all/" + quote(query, safe="") + "/date/1/")
        self.check_response(response)
        items = self.parse_search(response.text, base)
        semaphore = asyncio.Semaphore(3)
        failures = []

        async def detail(item):
            async with semaphore:
                try:
                    response = await client.get(item["detail"])
                    self.check_response(response)
                    results = self.parse_detail(response.text, item)
                    return results
                except Problem as exc:
                    failures.append(exc.message)
                    return []
                except httpx.HTTPError:
                    failures.append(self.network_message)
                    return []
        results = merge_results([r for batch in await asyncio.gather(*(detail(i) for i in items)) for r in batch])
        if items and not results and failures:
            message = next(message for message in (self.challenge_message, self.network_message,
                                                   self.page_message, self.magnet_message) if message in failures)
            raise Problem(message, 502)
        return results


def info_hash(value):
    return value.lower() if isinstance(value, str) and re.fullmatch(r"[A-Fa-f0-9]{40}", value) else None


class ReleaseFeed(Website):
    """Newest torrents of a site as homepage rows: one per info hash, keyed by the title's IMDb ID."""
    path, pages = "", 1

    async def latest(self):
        base = self.settings()[self.setting].rstrip("/")
        rows = []
        async with self.client() as client:
            for page in range(1, self.pages + 1):
                try:
                    async with asyncio.timeout(30):
                        response = await client.get(base + self.path, params=self.params(page))
                    response.raise_for_status()
                    rows.extend(self.parse(response.json()))
                except (httpx.HTTPError, TimeoutError, ValueError, Problem) as exc:
                    # Later pages only extend the window; keep what the first pages returned.
                    if rows:
                        break
                    if isinstance(exc, ValueError):
                        raise Problem(f"{self.name} 返回了无法识别的数据", 502) from None
                    if isinstance(exc, Problem):
                        raise
                    raise Problem(f"{self.name} 暂时无法访问或请求超时，请检查代理", 502) from None
        return rows


class Yts(ReleaseFeed):
    name, setting, path, pages = "YTS", "yts_url", "/api/v2/list_movies.json", 4

    @staticmethod
    def params(page):
        return {"sort_by": "date_added", "limit": 50, "page": page}

    @staticmethod
    def parse(data):
        body = data.get("data") if isinstance(data, dict) and data.get("status") == "ok" else None
        movies = body.get("movies", []) if isinstance(body, dict) else None
        if not isinstance(movies, list):
            raise Problem("YTS 返回了无法识别的数据", 502)
        rows = []
        for movie in movies:
            imdb = movie.get("imdb_code") if isinstance(movie, dict) else None
            if not isinstance(imdb, str) or not re.fullmatch(r"tt\d{5,10}", imdb):
                continue
            year, rating = movie.get("year"), movie.get("rating")
            for torrent in movie.get("torrents") or []:
                if not isinstance(torrent, dict):
                    continue
                hash_, published = info_hash(torrent.get("hash")), torrent.get("date_uploaded_unix")
                if not hash_ or not isinstance(published, (int, float)):
                    continue
                rows.append({"hash": hash_, "item": "imdb:" + imdb, "published": float(published), "source": "yts",
                             "title": str(movie.get("title") or "")[:120], "kind": "movie",
                             "year": year if isinstance(year, int) else None,
                             "quality": resolution(str(torrent.get("quality") or "")),
                             # YTS lists the IMDb rating; 0 means unrated.
                             "rating": rating if isinstance(rating, (int, float)) and rating > 0 else None})
        return rows


class Eztv(ReleaseFeed):
    name, setting, path, pages = "EZTV", "eztv_url", "/api/get-torrents", 3

    @staticmethod
    def params(page):
        return {"limit": 100, "page": page}

    @staticmethod
    def parse(data):
        torrents = data.get("torrents", []) if isinstance(data, dict) and "torrents_count" in data else None
        if not isinstance(torrents, list):
            raise Problem("EZTV 返回了无法识别的数据", 502)
        rows = []
        for torrent in torrents:
            if not isinstance(torrent, dict):
                continue
            imdb, hash_ = str(torrent.get("imdb_id") or ""), info_hash(torrent.get("hash"))
            published = torrent.get("date_released_unix")
            if not re.fullmatch(r"\d{5,10}", imdb) or not int(imdb) or not hash_ or not isinstance(published, int):
                continue
            title = str(torrent.get("title") or torrent.get("filename") or "")[:220]

            def number(value):
                return int(value) if str(value).isdigit() else None

            rows.append({"hash": hash_, "item": "imdb:tt" + imdb, "published": float(published), "source": "eztv",
                         "title": title, "kind": "tv", "quality": resolution(title),
                         "season": number(torrent.get("season")), "episode": number(torrent.get("episode"))})
        return rows


class WebRateLimit(Problem):
    def __init__(self, seconds):
        self.seconds = max(3, seconds)
        super().__init__("Telegram 公共网页限流，稍后自动继续", 429)


def public_username(peer):
    peer = peer.strip()
    if peer.startswith(("t.me/", "www.t.me/")):
        peer = "https://" + peer
    if "://" in peer:
        try:
            url = urlparse(peer)
        except ValueError:
            raise Problem("请填写有效的 Telegram 公开频道地址", 422) from None
        if url.scheme not in {"http", "https"} or url.hostname not in {"t.me", "www.t.me"} or url.username:
            raise Problem("请填写 Telegram 公开频道地址", 422)
        parts = url.path.strip("/").split("/")
        if parts[0] == "s":
            parts = parts[1:]
        if len(parts) != 1:
            raise Problem("请填写公开频道地址，不是消息或邀请链接", 422)
        peer = parts[0]
    peer = peer.removeprefix("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{3,32}", peer) or peer.isdigit() or peer in {"joinchat", "share", "login", "proxy"}:
        raise Problem("网页模式仅支持公开频道名称；私有频道和频道 ID 请使用 API 模式", 422)
    return peer


class TelegramWeb:
    def __init__(self, store):
        self.store = store
        self.lock = asyncio.Lock()
        self.last_request = 0
        self.stopped = False

    def client(self):
        return httpx.AsyncClient(timeout=18, proxy=self.store.settings()["proxy"] or None,
                                 trust_env=False, follow_redirects=True, headers={"User-Agent": "MediaLink/0.2"})

    @staticmethod
    def parse_page(body, username, revision=None):
        soup = BeautifulSoup(body, "html.parser")
        if not soup.select_one(".tgme_channel_info"):
            raise Problem("频道没有可访问的公开网页预览，请检查名称或使用 API 模式", 422)
        title = soup.select_one(".tgme_channel_info_header_title")
        title = title.get_text(" ", strip=True) if title else username
        messages, ids = [], []
        for node in soup.select(".tgme_widget_message[data-post]"):
            match = re.fullmatch(re.escape(username) + r"/(\d+)", node["data-post"], re.I)
            if not match:
                continue
            mid = int(match[1])
            if mid <= 0:
                continue
            ids.append(mid)
            text = node.select_one(".tgme_widget_message_text")
            if text:
                for br in text.select("br"):
                    br.replace_with("\n")
            text = text.get_text().strip() if text else ""
            links = [a["href"] for a in node.select("a[href]")]
            date = node.select_one("time[datetime]")
            published = published_date(date["datetime"]) if date else None
            origin = {"provider": "telegram", "name": title, "url": f"https://t.me/{username}/{mid}"}
            resources = extract_resources(text, links, origin, published)
            # Unsupported/hidden posts must not erase a previously readable message.
            if text or resources:
                messages.append({"message_id": mid, "body": text, "resources": resources,
                                 "published": published, "revision": revision if revision is not None else time.time()})
        before = None
        for more in soup.select("a.tme_messages_more[href]"):
            url = urlparse(urljoin("https://t.me", more["href"]))
            value = parse_qs(url.query).get("before", [""])[0]
            if url.hostname == "t.me" and url.path.rstrip("/").lower() == f"/s/{username}" and value.isdigit():
                before = int(value)
                break
        if before is not None and (before <= 0 or (ids and before > min(ids))):
            raise Problem("Telegram 网页翻页标识无效，已保留当前进度", 502)
        return {"title": title, "messages": messages, "ids": ids, "before": before}

    async def fetch_page(self, username, before=None):
        async with self.lock:
            blocked = max((c["wait_until"] for c in self.store.channels()
                           if c["mode"] == "public_web" and c["status"] == "公共网页限流等待"), default=0)
            if blocked > time.time():
                raise WebRateLimit(blocked - time.time())
            await asyncio.sleep(max(0, 3 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                async with self.client() as client:
                    response = await client.get(f"https://t.me/s/{username}", params={"before": before} if before else {})
                if response.status_code == 429 or (response.status_code == 503 and "Retry-After" in response.headers):
                    retry = response.headers.get("Retry-After", "60")
                    try:
                        seconds = float(retry)
                    except ValueError:
                        try:
                            seconds = parsedate_to_datetime(retry).timestamp() - time.time()
                        except (ValueError, TypeError):
                            seconds = 60
                    raise WebRateLimit(seconds)
                response.raise_for_status()
            except httpx.HTTPError:
                raise Problem("Telegram 公共网页暂时无法访问，请检查代理", 502) from None
            page = self.parse_page(response.text, username)
            if before and page["before"] is not None and page["before"] >= before:
                raise Problem("Telegram 网页未向前翻页，已保留当前进度", 502)
            return page

    async def add_channel(self, peer):
        username = public_username(peer)
        page = await self.fetch_page(username)
        channel_id = "web:" + username
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO channels(id,peer,title,mode) VALUES (?,?,?,?)",
                       (channel_id, username, page["title"], "public_web"))
        return self.store.channel(channel_id)

    async def sync_channel(self, channel):
        now = time.time()
        refreshing = bool(channel["refresh_before"] or channel["refresh_at"] <= now)
        if not refreshing and channel["complete"]:
            return
        before = channel["refresh_before"] if refreshing else channel["cursor"]
        page = await self.fetch_page(channel["peer"], before or None)
        ids, older = page["ids"], page["before"]
        updates = {"title": page["title"], "last_synced": time.time(), "error": None, "wait_until": 0, "failures": 0}
        if refreshing:
            newest = max([channel["highwater"], channel["refresh_highwater"], *ids])
            if channel["highwater"] and (not ids or min(ids) > channel["highwater"]) and older:
                updates.update(refresh_before=older, refresh_highwater=newest, status="正在补齐新消息")
            else:
                updates.update(highwater=newest, refresh_before=0, refresh_highwater=0, refresh_at=now + 300,
                               status="可见历史回溯完成 · 定时检查新消息" if channel["complete"] else "正在回溯网页可见历史")
                if not channel["highwater"] and not channel["cursor"]:
                    updates.update(cursor=older or (min(ids) if ids else 0), complete=int(older is None))
                    if older is None:
                        updates["status"] = "可见历史回溯完成 · 定时检查新消息"
        else:
            updates.update(cursor=older or (min(ids) if ids else channel["cursor"]), complete=int(older is None),
                           status="可见历史回溯完成 · 定时检查新消息" if older is None else "正在回溯网页可见历史")
        await blocking(self.store.commit_web_page, channel["id"], page["messages"], updates)

    async def run_once(self):
        for channel in self.store.channels():
            if channel["mode"] != "public_web" or not channel["enabled"] or channel["wait_until"] > time.time():
                continue
            try:
                await self.sync_channel(channel)
            except Exception as error:
                try:
                    current = self.store.channel(channel["id"])
                except Problem:
                    continue
                failures = current["failures"] + 1
                delay = error.seconds if isinstance(error, WebRateLimit) else min(30 * 2 ** min(failures - 1, 6), 1800)
                self.store.channel(channel["id"], failures=failures, wait_until=time.time() + delay,
                                   status="公共网页限流等待" if isinstance(error, WebRateLimit) else "网页同步等待重试",
                                   error=error.message if isinstance(error, Problem) else "网页同步失败，稍后自动重试")

    async def run(self):
        while not self.stopped:
            await self.run_once()
            await asyncio.sleep(1)
