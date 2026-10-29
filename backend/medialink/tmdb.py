import asyncio
import hashlib
import json
import os
import re
import secrets
import time
import unicodedata
from datetime import datetime
from email.utils import parsedate_to_datetime

import httpx

from .core import Problem, blocking


def text(value):
    return value.strip() if isinstance(value, str) else ""


def normalize_title(value):
    return re.sub(r"[\W_]", "", unicodedata.normalize("NFKC", text(value)).casefold())


def query_title(query):
    cleaned = unicodedata.normalize("NFKC", query).strip()
    matches = list(re.finditer(r"(?<=[\s(\[])(19\d{2}|20\d{2})(?=[)\]\s]*$)", cleaned))
    for match in reversed(matches):
        remaining = (cleaned[: match.start()] + cleaned[match.end() :]).strip(" ()（）[]-_")
        year = int(match[0])
        if remaining and year <= datetime.now().year + 2:
            return remaining, year
    return cleaned, None


def unique_queries(values):
    found, result = set(), []
    for value in values:
        key = normalize_title(value)
        if key and key not in found:
            found.add(key)
            result.append(value.strip()[:200])
    return result[:4]


def chinese_number(number):
    digits = "零一二三四五六七八九"
    if number < 10:
        return digits[number]
    if number < 100:
        tens, units = divmod(number, 10)
        return (digits[tens] if tens > 1 else "") + "十" + (digits[units] if units else "")
    return str(number)


def media_queries(query, media, season_number=None):
    if season_number is None:
        return unique_queries(
            [query, media.get("chinese_title"), media.get("english_title"), media.get("original_title")]
        )
    titles = [media.get("chinese_title"), media.get("english_title"), media.get("original_title")]
    values = []
    if titles[0]:
        season_name = next(
            (text(s.get("name")) for s in media.get("seasons", []) if s.get("number") == season_number), ""
        )
        if re.search(r"[\u3400-\u9fff]", season_name):
            values.append(f"{titles[0]} {season_name}")
        values.extend([f"{titles[0]} 第{chinese_number(season_number)}季", f"{titles[0]} 第{season_number}季"])
    values.extend(f"{title} S{season_number:02d}" for title in titles[1:] if title)
    return unique_queries(values or [f"{media['title']} S{season_number:02d}"])


def image_url(path, size="w500"):
    return (
        f"/api/v1/tmdb/images/{size}/{path[1:]}"
        if re.fullmatch(r"/[A-Za-z0-9_-]+\.(?:jpg|png|webp)", text(path))
        else None
    )


def summary(data, media_type=None):
    kind = data.get("media_type", media_type)
    title = text(data.get("title" if kind == "movie" else "name"))
    date = text(data.get("release_date" if kind == "movie" else "first_air_date"))
    original = text(data.get("original_title" if kind == "movie" else "original_name"))
    return {
        "id": data["id"],
        "type": kind,
        "title": title or original or "未知片名",
        "chinese_title": title if re.search(r"[\u3400-\u9fff]", title) else None,
        "english_title": None,
        "original_title": original or None,
        "year": int(date[:4]) if re.match(r"\d{4}-", date) else None,
        "poster": image_url(data.get("poster_path")),
        "backdrop": image_url(data.get("backdrop_path"), "w1280"),
        "rating": data.get("vote_average") or None,
        "votes": data.get("vote_count") or 0,
        "overview": text(data.get("overview")),
    }


class TMDB:
    def __init__(self, store, vault):
        self.store, self.vault = store, vault
        self.wait_until = 0
        self.semaphore = asyncio.Semaphore(4)
        self.lock = asyncio.Lock()
        self.image_lock = asyncio.Lock()
        self.image_dir = store.path.parent / "tmdb-images"
        self.image_limit = 256 * 1024 * 1024

    async def credential(self):
        raw = await blocking(self.vault.get, "tmdb-credential")
        if not raw:
            raise Problem("请先在连接设置中配置 TMDB 凭据", 503)
        try:
            value = json.loads(raw)
            if value["type"] not in {"token", "api_key"} or not text(value["value"]):
                raise ValueError
            return value
        except (ValueError, KeyError, TypeError):
            raise Problem("TMDB 凭据无效，请在连接设置中重新保存", 503) from None

    async def state(self):
        try:
            value = await self.credential()
            return {"tmdb_configured": True, "tmdb_auth_type": value["type"]}
        except Problem:
            return {"tmdb_configured": False, "tmdb_auth_type": "token"}

    async def configure(self, value):
        if value is None:
            await blocking(self.vault.delete, "tmdb-credential")
        else:
            if (
                not isinstance(value, dict)
                or set(value) != {"type", "value"}
                or not isinstance(value["type"], str)
                or value["type"] not in {"token", "api_key"}
                or not isinstance(value["value"], str)
                or not 1 <= len(value["value"].strip()) <= 4096
                or re.search(r"\s", value["value"].strip())
            ):
                raise Problem("请填写有效的 TMDB 凭据并选择授权类型", 422)
            await blocking(
                self.vault.set, "tmdb-credential", json.dumps({"type": value["type"], "value": value["value"].strip()})
            )
        self.wait_until = 0

    def client(self):
        return httpx.AsyncClient(
            timeout=18, proxy=self.store.settings()["proxy"] or None, trust_env=False, follow_redirects=False
        )

    @staticmethod
    def envelope(cached, stale=False):
        return {"data": cached["data"], "updated": cached["updated"], "stale": stale}

    def rate_limit(self, response):
        delay = 60
        try:
            value = response.headers.get("retry-after", "60")
            delay = float(value) if value.isdigit() else parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            pass
        self.wait_until = max(self.wait_until, time.time() + max(1, min(delay, 86400)))

    async def fetch(self, path, params=None, ttl=86400):
        credential = await self.credential()
        params = params or {}
        key = "tmdb:" + path + ":" + json.dumps(params, sort_keys=True, ensure_ascii=False)
        cached = await blocking(self.store.cached_metadata, key)
        if cached and cached["expires"] > time.time():
            return self.envelope(cached)
        if self.wait_until > time.time():
            if cached:
                return self.envelope(cached, True)
            raise Problem("TMDB 正在限流，请稍后重试", 429)
        # A bounded lock also coalesces duplicate requests for the same cached data.
        async with self.lock:
            cached = await blocking(self.store.cached_metadata, key)
            if cached and cached["expires"] > time.time():
                return self.envelope(cached)
            if self.wait_until > time.time():
                if cached:
                    return self.envelope(cached, True)
                raise Problem("TMDB 正在限流，请稍后重试", 429)
            headers, request_params = {}, dict(params)
            if credential["type"] == "token":
                headers["Authorization"] = "Bearer " + credential["value"]
            else:
                request_params["api_key"] = credential["value"]
            try:
                async with self.client() as client:
                    response = await client.get(
                        "https://api.themoviedb.org/3" + path, params=request_params, headers=headers
                    )
                if response.status_code in {401, 403}:
                    raise Problem("TMDB 凭据无效或无访问权限，请更新凭据", 502)
                if response.status_code == 404:
                    raise Problem("TMDB 影片或资料不存在", 404)
                if response.status_code == 429:
                    self.rate_limit(response)
                    if cached:
                        return self.envelope(cached, True)
                    raise Problem("TMDB 正在限流，请稍后重试", 429)
                response.raise_for_status()
                data = response.json()
                expected = list if path in {"/configuration/countries", "/configuration/languages"} else dict
                if not isinstance(data, expected):
                    raise ValueError
            except (httpx.HTTPError, ValueError):
                if cached:
                    return self.envelope(cached, True)
                raise Problem("TMDB 暂时无法访问或返回了无效数据，请检查代理", 502) from None
            updated = await blocking(self.store.cache_metadata, key, data, ttl)
            return {"data": data, "updated": updated, "stale": False}

    async def check(self):
        await self.credential()
        # Do not report an old cached reply as a successful credential check.
        result = await self.fetch("/authentication", ttl=0)
        if result["stale"] or not result["data"].get("success"):
            raise Problem("TMDB 当前无法完成授权检测，请稍后重试", 502)
        return {"ok": True, "message": "TMDB 授权有效"}

    async def trending(self, media_type="all", window="week", page=1):
        if media_type not in {"all", "movie", "tv"} or window not in {"day", "week"} or not 1 <= page <= 500:
            raise Problem("热门筛选参数无效", 422)
        response = await self.fetch(f"/trending/{media_type}/{window}", {"language": "zh-CN", "page": page}, 1800)
        raw = response["data"]
        items = [
            summary(item, media_type)
            for item in raw.get("results", [])
            if isinstance(item, dict)
            and item.get("media_type", media_type) in {"movie", "tv"}
            and isinstance(item.get("id"), int)
            and not item.get("adult")
        ]
        return {
            "items": items,
            "page": page,
            "total_pages": min(raw.get("total_pages", 1), 500),
            "updated": response["updated"],
            "stale": response["stale"],
        }

    async def filters(self, media_type="movie"):
        if media_type not in {"movie", "tv"}:
            raise Problem("影视类别无效", 422)
        replies = [
            await self.fetch(f"/genre/{media_type}/list", {"language": "zh-CN"}),
            await self.fetch("/configuration/countries", {"language": "zh-CN"}),
            await self.fetch("/configuration/languages"),
        ]
        genres = replies[0]["data"].get("genres")
        if not isinstance(genres, list) or not genres:
            raise Problem("TMDB 类型资料无效", 502)
        language_names = {
            "zh": "中文", "cn": "粤语", "en": "英语", "ja": "日语", "ko": "韩语",
            "fr": "法语", "de": "德语", "es": "西班牙语", "it": "意大利语",
            "ru": "俄语", "th": "泰语", "hi": "印地语", "pt": "葡萄牙语",
            "sv": "瑞典语", "da": "丹麦语", "pl": "波兰语", "id": "印尼语",
            "no": "挪威语", "ar": "阿拉伯语", "xx": "无对白",
        }
        country_names = {"CN": "中国大陆", "HK": "香港", "TW": "台湾"}
        try:
            result = {
                "genres": [{"id": g["id"], "name": g["name"]} for g in genres],
                "countries": [{"id": c["iso_3166_1"], "name": country_names.get(c["iso_3166_1"])
                               or c.get("native_name") or c["english_name"]} for c in replies[1]["data"]],
                "languages": [{"id": lang["iso_639_1"], "name": language_names.get(lang["iso_639_1"])
                               or lang.get("name") or lang["english_name"]} for lang in replies[2]["data"]],
                "updated": min(r["updated"] for r in replies),
                "stale": any(r["stale"] for r in replies),
            }
            if any(not isinstance(g["id"], int) or not text(g["name"]) for g in result["genres"]):
                raise ValueError
            if any(not re.fullmatch(r"[A-Z]{2}", c["id"]) or not text(c["name"]) for c in result["countries"]):
                raise ValueError
            if any(not re.fullmatch(r"[a-z]{2}", lang["id"]) or not text(lang["name"]) for lang in result["languages"]):
                raise ValueError
            if not result["countries"] or not result["languages"]:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise Problem("TMDB 筛选资料无效，请稍后重试", 502) from None
        return result

    async def discover(self, media_type="movie", include_genres="", exclude_genres="",
                       year_from=None, year_to=None, country="", original_language="", page=1):
        if media_type not in {"movie", "tv"} or not 1 <= page <= 500:
            raise Problem("分类浏览参数无效", 422)
        def genre_ids(value):
            if not value:
                return set()
            if len(value) > 200 or not re.fullmatch(r"[0-9]+(?:,[0-9]+)*", value):
                raise Problem("类型筛选参数无效", 422)
            return {int(v) for v in value.split(",")}
        included, excluded = genre_ids(include_genres), genre_ids(exclude_genres)
        if included & excluded:
            raise Problem("同一类型不能同时包含和排除", 422)
        for year in (year_from, year_to):
            if year is not None and (not isinstance(year, int) or not 1000 <= year <= 9999):
                raise Problem("年份必须在 1000 至 9999 之间", 422)
        if year_from is not None and year_to is not None and year_from > year_to:
            raise Problem("起始年份不能晚于结束年份", 422)
        options = await self.filters(media_type)
        if not (included | excluded) <= {g["id"] for g in options["genres"]}:
            raise Problem("当前影视类别不支持所选类型", 422)
        if country and country not in {c["id"] for c in options["countries"]}:
            raise Problem("出品地区无效", 422)
        if original_language and original_language not in {lang["id"] for lang in options["languages"]}:
            raise Problem("原始语言无效", 422)
        params = {"language": "zh-CN", "include_adult": False, "sort_by": "popularity.desc", "page": page}
        if included:
            params["with_genres"] = ",".join(str(g) for g in sorted(included))
        if excluded:
            params["without_genres"] = ",".join(str(g) for g in sorted(excluded))
        date_key = "primary_release_date" if media_type == "movie" else "first_air_date"
        if year_from is not None:
            params[date_key + ".gte"] = f"{year_from:04d}-01-01"
        if year_to is not None:
            params[date_key + ".lte"] = f"{year_to:04d}-12-31"
        if country:
            params["with_origin_country"] = country
        if original_language:
            params["with_original_language"] = original_language
        reply = await self.fetch(f"/discover/{media_type}", params, 1800)
        raw = reply["data"]
        if (not isinstance(raw.get("results"), list) or not isinstance(raw.get("total_pages"), int)
                or raw["total_pages"] < 0):
            raise Problem("TMDB 返回了无效的分类浏览资料", 502)
        return {
            "items": [summary(item, media_type) for item in raw["results"]
                      if isinstance(item, dict) and isinstance(item.get("id"), int) and not item.get("adult")],
            "page": page, "total_pages": min(raw["total_pages"], 500),
            "updated": reply["updated"], "stale": reply["stale"],
        }

    async def search(self, query, page=1):
        if not text(query) or len(query) > 200 or not 1 <= page <= 500:
            raise Problem("TMDB 搜索参数无效", 422)
        replies = []
        for language in ("zh-CN", "en-US"):
            replies.append(
                await self.fetch(
                    "/search/multi",
                    {"query": query.strip(), "language": language, "page": page, "include_adult": False},
                    600,
                )
            )
        items = {}
        for index, reply in enumerate(replies):
            for hit in reply["data"].get("results", []):
                if (
                    not isinstance(hit, dict)
                    or hit.get("media_type") not in {"movie", "tv"}
                    or not isinstance(hit.get("id"), int)
                    or hit.get("adult")
                ):
                    continue
                key = (hit["media_type"], hit["id"])
                item = items.setdefault(key, summary(hit))
                if index:
                    en = text(hit.get("title" if key[0] == "movie" else "name"))
                    if (
                        en
                        and not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", en)
                        and (
                            hit.get("original_language") == "en"
                            or normalize_title(en) != normalize_title(item["original_title"])
                        )
                    ):
                        item["english_title"] = en
        return {
            "items": list(items.values()),
            "page": page,
            "total_pages": min(max(r["data"].get("total_pages", 1) for r in replies), 500),
            "updated": min(r["updated"] for r in replies),
            "stale": any(r["stale"] for r in replies),
        }

    async def find_imdb(self, imdb_id):
        """The movie or show behind an IMDb ID; episode IDs resolve to their show."""
        if not isinstance(imdb_id, str) or not re.fullmatch(r"tt\d{5,10}", imdb_id):
            raise Problem("IMDb 编号无效", 422)
        # Misses are cached too, so new titles that TMDB has not mapped yet are retried after a few days.
        data = (await self.fetch(f"/find/{imdb_id}", {"external_source": "imdb_id"}, 3 * 86400))["data"]
        for kind in ("movie", "tv"):
            for hit in data.get(kind + "_results") or []:
                if isinstance(hit, dict) and isinstance(hit.get("id"), int) and not hit.get("adult"):
                    return {"type": kind, "id": hit["id"]}
        for hit in data.get("tv_episode_results") or []:
            if isinstance(hit, dict) and isinstance(hit.get("show_id"), int):
                return {"type": "tv", "id": hit["show_id"]}
        return None

    async def detail(self, media_type, media_id):
        if media_type not in {"movie", "tv"} or not isinstance(media_id, int) or media_id <= 0:
            raise Problem("影片标识无效", 422)
        reply = await self.fetch(
            f"/{media_type}/{media_id}", {"language": "zh-CN", "append_to_response": "translations,credits"}
        )
        raw = reply["data"]
        if raw.get("id") != media_id:
            raise Problem("TMDB 返回了无效的影片资料", 502)
        result = summary(raw, media_type)
        name_key = "title" if media_type == "movie" else "name"
        translations = raw.get("translations", {}).get("translations", [])
        zh, en = {}, {}
        for language, countries in (("zh", ("CN", "SG", "TW", "HK")), ("en", ("US", "GB"))):
            available = [t for t in translations if t.get("iso_639_1") == language and isinstance(t.get("data"), dict)]
            available.sort(
                key=lambda t: countries.index(t.get("iso_3166_1")) if t.get("iso_3166_1") in countries else 99
            )
            picked = next(
                (t["data"] for t in available if text(t["data"].get(name_key))),
                available[0]["data"] if available else {},
            )
            if language == "zh":
                zh = picked
            else:
                en = picked
        result["chinese_title"] = text(zh.get(name_key)) or result["chinese_title"]
        result["english_title"] = text(en.get(name_key)) or (
            result["original_title"] if raw.get("original_language") == "en" else None
        )
        overview = text(zh.get("overview")) or text(raw.get("overview"))
        result["overview_language"] = "zh" if re.search(r"[\u3400-\u9fff]", overview) else "en"
        if not overview or result["overview_language"] == "en":
            overview = text(en.get("overview")) or overview
        if not result["english_title"] or not overview:
            try:
                english = await self.fetch(f"/{media_type}/{media_id}", {"language": "en-US"})
                english_title = text(english["data"].get(name_key))
                if (
                    not result["english_title"]
                    and not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", english_title)
                    and (
                        raw.get("original_language") == "en"
                        or normalize_title(english_title) != normalize_title(result["original_title"])
                    )
                ):
                    result["english_title"] = english_title or None
                if not overview:
                    overview = text(english["data"].get("overview"))
                    result["overview_language"] = "en"
                reply["stale"] |= english["stale"]
                reply["updated"] = min(reply["updated"], english["updated"])
            except Problem:
                result["warning"] = "部分英文资料暂不可用"
        result["title"] = (
            result["chinese_title"] or result["english_title"] or result["original_title"] or result["title"]
        )
        credits = raw.get("credits", {})
        crew = (
            [text(p.get("name")) for p in credits.get("crew", []) if p.get("job") == "Director"]
            if media_type == "movie"
            else [text(p.get("name")) for p in raw.get("created_by", [])]
        )
        result.update(
            {
                "overview": overview,
                "genres": [text(g.get("name")) for g in raw.get("genres", [])],
                "runtime": raw.get("runtime") or None,
                "creators": list(dict.fromkeys(n for n in crew if n)),
                "cast": [
                    {
                        "name": text(p.get("name")),
                        "character": text(p.get("character")),
                        "portrait": image_url(p.get("profile_path"), "w185"),
                    }
                    for p in credits.get("cast", [])[:10]
                ],
                "seasons": [
                    {
                        "number": s["season_number"],
                        "name": text(s.get("name")),
                        "episodes": s.get("episode_count"),
                        "year": text(s.get("air_date"))[:4] or None,
                    }
                    for s in raw.get("seasons", [])
                    if isinstance(s.get("season_number"), int)
                ],
                "season_count": raw.get("number_of_seasons"),
                "episode_count": raw.get("number_of_episodes"),
                "last_air_date": text(raw.get("last_air_date")) or None,
                "genre_ids": [g["id"] for g in raw.get("genres", []) if isinstance(g.get("id"), int)],
                "countries": [c for c in raw.get("origin_country") or
                              [p.get("iso_3166_1") for p in raw.get("production_countries", [])]
                              if isinstance(c, str)],
                "original_language": text(raw.get("original_language")) or None,
                "popularity": raw.get("popularity") if isinstance(raw.get("popularity"), (int, float)) else 0,
                "updated": reply["updated"],
                "stale": reply["stale"],
            }
        )
        return result

    def read_image(self, file):
        if file.exists():
            data = file.read_bytes()
            os.utime(file, None)
            return data
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
        files = sorted(self.image_dir.glob("*.image"), key=lambda p: p.stat().st_mtime, reverse=True)
        total = 0
        for cached in files:
            total += cached.stat().st_size
            if total > self.image_limit:
                cached.unlink(missing_ok=True)

    async def image(self, size, path):
        if size not in {"w185", "w342", "w500", "w780", "w1280", "original"} or not re.fullmatch(
            r"[A-Za-z0-9_-]+\.(?:jpg|png|webp)", path
        ):
            raise Problem("图片路径或尺寸无效", 422)
        file = self.image_dir / (hashlib.sha256((size + "/" + path).encode()).hexdigest() + ".image")
        mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[path.rsplit(".", 1)[1]]
        async with self.semaphore:
            async with self.image_lock:
                cached = await blocking(self.read_image, file)
            if cached:
                return cached, mime
            if self.wait_until > time.time():
                raise Problem("TMDB 图片正在限流，请稍后重试", 429)
            try:
                async with (
                    self.client() as client,
                    client.stream("GET", f"https://image.tmdb.org/t/p/{size}/{path}") as response,
                ):
                    if response.status_code == 429:
                        self.rate_limit(response)
                        raise Problem("TMDB 图片正在限流，请稍后重试", 429)
                    response.raise_for_status()
                    if response.headers.get("content-type", "").split(";", 1)[0] != mime:
                        raise Problem("TMDB 返回了无效的图片", 502)
                    chunks, length = [], 0
                    async for chunk in response.aiter_bytes():
                        length += len(chunk)
                        if length > 12 * 1024 * 1024:
                            raise Problem("图片超过大小限制", 502)
                        chunks.append(chunk)
                data = b"".join(chunks)
                if not data:
                    raise Problem("TMDB 返回了空图片", 502)
                async with self.image_lock:
                    await blocking(self.save_image, file, data)
                return data, mime
            except httpx.HTTPError:
                raise Problem("TMDB 图片暂时无法加载", 502) from None
