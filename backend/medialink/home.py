"""Cached homepage feed built from independent sources, with shared TMDB title matching."""

import asyncio
import re
import time

from .core import Problem, blocking
from .tmdb import normalize_title

REFRESH_SECONDS = 30 * 60
RETRY_SECONDS = 5 * 60
TICK_SECONDS = 60
LOCAL_KEY = "home:local"
LOCAL_MESSAGE = "首页由 Telegram 频道、YTS、EZTV 和蜜柑计划的最新资源整理"
LOCAL_TIMEOUT_MESSAGE = "首页资源整理超时，请检查代理或稍后重试"
MATCH_TTL, MISS_TTL = 30 * 86400, 86400
# Titles that only have trailers are left off the homepage.
HIDDEN_STATUSES = {"预告"}
SEASON = re.compile(r"\s*(?:第[一二三四五六七八九十百\d]+(?:[季部期]|クール)|年番\d*|\d+(?:st|nd|rd|th)\s*Season|Season\s*\d+"
                    r"|S\d{1,2})\s*$", re.I)


def base_title(title):
    return SEASON.sub("", title).strip() or title


def match_tmdb(item, candidates):
    """Pick the TMDB summary whose title matches exactly and whose year is plausible."""
    kinds = {"movie": {"movie"}, "tv": {"tv"}}.get(item["kind"], {"movie", "tv"})
    wanted = {normalize_title(value) for value in (item["title"], base_title(item["title"]),
                                                   item.get("english_title")) if value} - {""}
    # Other names (bgm.tv aliases) may be short abbreviations; single characters are too ambiguous.
    wanted |= {value for name in item.get("names", ()) for value in map(normalize_title, (name, base_title(name)))
               if len(value) > 1}
    year, found = item.get("year"), []
    for candidate in candidates:
        if candidate.get("type") not in kinds:
            continue
        names = {normalize_title(candidate.get(key)) for key in
                 ("title", "chinese_title", "english_title", "original_title") if candidate.get(key)}
        if not wanted & names:
            continue
        dated = candidate.get("year")
        if year and dated:
            # A later season of a show is dated by the season, not the first air date.
            if dated > year + 1 or (candidate["type"] == "movie" and dated < year - 1):
                continue
        found.append(candidate)
    if not found:
        return None
    # Remakes share the original's title: the closest year wins, an undated entry counts as a year off.
    best = min(found, key=lambda c: abs(c["year"] - year) if year and c.get("year") else 1)
    return {"type": best["type"], "id": best["id"]}


async def search_tmdb(tmdb, item, queries):
    for query in dict.fromkeys(base_title(q) for q in queries if q):
        match = match_tmdb(item, (await tmdb.search(query))["items"])
        if match:
            return match
    return None


async def link_tmdb(store, tmdb, key, item, network, names=None, version=1):
    """Cached TMDB link for a listed title; without network only cached links are used.

    `names` lazily supplies other names of the title (original name first) for when its own titles miss;
    misses cached before `version` are searched again.
    """
    cached = await blocking(store.cached_metadata, key)
    if cached and cached["expires"] > time.time() and (cached["data"].get("media")
                                                         or cached["data"].get("version", 1) >= version):
        return cached["data"].get("media")
    if not network:
        return None
    tried = [item["title"], item.get("english_title")]
    match, searched = await search_tmdb(tmdb, item, tried), version
    if not match and names:
        try:
            others = await names()
        except Problem:
            # Retry the other names on the next pass.
            others, searched = [], 1
        tried = {normalize_title(base_title(q)) for q in tried if q}
        if others:
            item = {**item, "names": others}
            match = await search_tmdb(tmdb, item, [q for q in others
                                                   if normalize_title(base_title(q)) not in tried][:2])
    await blocking(store.cache_metadata, key, {"media": match, "version": searched},
                   MATCH_TTL if match else MISS_TTL)
    return match


def visible(item):
    counts = item.get("counts") or {}
    return item.get("status") not in HIDDEN_STATUSES and (counts.get("magnet") or counts.get("cloud"))


class HomeFeed:
    """Serve and refresh the independent local feed, preserving cached results on failure."""

    def __init__(self, store, local):
        self.store, self.local = store, local
        self.lock = asyncio.Lock()
        self.error = None
        self.retry_at = 0
        self.stopped = False

    async def cached(self):
        return await blocking(self.store.cached_metadata, LOCAL_KEY)

    async def feed(self):
        cached = await self.cached()
        data = cached["data"] if cached else {"featured": [], "sections": []}
        updated = cached["updated"] if cached else None
        return {
            "source": "local", "status": "ready", "message": LOCAL_MESSAGE, "error": self.error,
            "warnings": data.get("errors", []), "updated": updated, "refreshing": self.lock.locked(),
            "stale": bool(cached) and (self.error is not None or updated < time.time() - 2 * REFRESH_SECONDS),
            "featured": data["featured"],
            "sections": [{**section, "items": [item for item in section["items"] if visible(item)]}
                         for section in data["sections"]],
        }

    async def refresh(self, force=False):
        async with self.lock:
            cached = await self.cached()
            if cached and not force and cached["updated"] > time.time() - REFRESH_SECONDS:
                return
            try:
                data = await self.local.home()
            except Problem as exc:
                self.error, self.retry_at = exc.message, time.time() + RETRY_SECONDS
                raise
            except TimeoutError:
                self.error, self.retry_at = LOCAL_TIMEOUT_MESSAGE, time.time() + RETRY_SECONDS
                raise Problem(LOCAL_TIMEOUT_MESSAGE, 502) from None
            self.error, self.retry_at = None, 0
            # The local builder has already linked titles to TMDB.
            await blocking(self.store.cache_metadata, LOCAL_KEY, data, REFRESH_SECONDS)

    async def run(self):
        while not self.stopped:
            if time.time() >= self.retry_at:
                try:
                    await self.refresh()
                except Problem:
                    pass
            await asyncio.sleep(TICK_SECONDS)
