"""Homepage of recently updated titles built from MediaLink's independent sources.

Each source reports that a title gained resources at some time: posts of the synced Telegram channels (115
shares), the newest YTS movies and EZTV episodes, and the Mikan anime schedule. Titles are linked to TMDB, merged,
filtered by release date, rating and popularity (recent years, rated 6 or better, no cam-only copies,
talk or reality shows) and ordered by their newest resource. Within a day, titles with 115 shares come
first: they transfer instantly, while magnets need an offline download.
"""

import re
import time
from collections import Counter
from datetime import datetime, timezone

from .anime import MIKAN_TIME, release_episode, release_resolution
from .core import Problem, blocking
from .home import base_title, link_tmdb
from .search_jobs import QUALITY_ORDER
from .sources import resolution
from .tmdb import normalize_title

WINDOW = 14 * 86400
FEATURED_WINDOW = 7 * 86400
SECTION_SIZE = 12
# Titles are linked in display order until every section is full; this bounds TMDB lookups per refresh.
MAX_LINKED = 160
RECENT_YEARS = 3
MIN_RATING, RATED_VOTES = 6, 20
# TMDB popularity undervalues some Chinese titles, so curated channel posts get a lower floor
# than titles seen only on YTS, EZTV or Mikan.
CURATED_POPULARITY, OPEN_POPULARITY = 3, 20
# Long-running shows seen only on EZTV need an audience; this skips daytime TV that started decades ago.
ESTABLISHED_VOTES = 100
ANIMATION = 16
# Exclude news, reality, soap and talk shows.
HIDDEN_GENRES = {10763, 10764, 10766, 10767}
ANIME_ORIGINS, ANIME_LANGUAGES = {"JP", "CN", "KR"}, {"ja", "zh", "ko"}
# Mikan feeds are read for the most recently updated shows, at most this often unless the show updates again.
MIKAN_FEED_TTL = 6 * 3600
SECTIONS = (("movie", "最近更新的电影"), ("tv", "最近更新的剧集"), ("anime", "最近更新的动漫"))
# TMDB leaves some series genres untranslated.
GENRES = {"Sci-Fi & Fantasy": "科幻", "War & Politics": "战争", "Action & Adventure": "动作冒险", "Kids": "儿童"}
COUNTRIES = {"CN": "大陆", "HK": "香港", "TW": "台湾", "US": "美国", "GB": "英国", "JP": "日本", "KR": "韩国",
             "FR": "法国", "DE": "德国", "IN": "印度", "TH": "泰国", "CA": "加拿大", "ES": "西班牙", "IT": "意大利",
             "AU": "澳大利亚", "RU": "俄罗斯"}
# Channel posts open with "🎬 片名 (2026) …" for films and "📺 片名 (2026) S01E16 …" or "… 第1季 更新至第7集"
# for series; some prefix the category, as in "📺 电视剧｜片名 (2023) …".
POST = re.compile(r"^\s*(?P<icon>🎬|📺)?\s*(?:(?:电视剧|电影|动漫|动画|综艺|纪录片)\s*[｜|]\s*)?"
                  r"(?P<title>[^\n(（]{1,80}?)\s*[(（](?P<year>(?:19|20)\d{2})[)）](?P<rest>[^\n]*)")
EPISODE = re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})E(\d{1,4})(?!\d)", re.I)
SEASON = re.compile(r"第\s*([零一二三四五六七八九十\d]{1,3})\s*季|(?<![A-Za-z0-9])S(\d{1,2})(?![\dE])", re.I)
NUMBER = re.compile(r"(?:更新至|第)\s*(\d{1,4})\s*[集话話]")
TOTAL = re.compile(r"全\s*(\d{1,4})\s*[集话話]")
TMDB_ID = re.compile(r"\{tmdbid-(\d{1,9})\}")
DIGITS = "零一二三四五六七八九"
CAM = re.compile(r"(?<![A-Za-z])(?:HD)?(?:CAM|TS|TC)(?:RIP)?(?![A-Za-z])|枪版", re.I)
DOLBY_VISION = re.compile(r"杜比视界|Dolby\s*Vision|(?<![A-Za-z])(?:DoVi|DV)(?![A-Za-z])", re.I)


def numeral(value):
    """Arabic or Chinese season numbers up to 99."""
    if value.isdigit():
        return int(value)
    tens, ten, ones = value.partition("十")
    try:
        return (DIGITS.index(tens or "一") * 10 + (DIGITS.index(ones) if ones else 0) if ten
                else DIGITS.index(value) if len(value) == 1 else None)
    except ValueError:
        return None


def parse_post(body):
    """Title, year and release details of a channel post, or None for posts in other formats such as ads.

    ``episode`` is ``(season, number)``; a season pack without an episode number has number 0.
    """
    first = next((line for line in (body or "").splitlines() if line.strip()), "")
    match = POST.match(first)
    title = re.sub(r"^[\W_]+", "", match["title"]).strip() if match else ""
    if not title:
        return None
    rest = match["rest"]
    episode, total, tmdb_id = EPISODE.search(rest), TOTAL.search(rest), TMDB_ID.search(rest)
    season, number = SEASON.search(rest), NUMBER.search(rest)
    season = (numeral(season[1]) if season[1] else int(season[2])) if season else None
    if episode:
        episode = (int(episode[1]), int(episode[2]))
    elif number or season:
        episode = (season or 1, int(number[1]) if number else 0)
    kind = "tv" if episode or total or match["icon"] == "📺" else "movie" if match["icon"] == "🎬" else None
    return {"title": title[:80], "year": int(match["year"]), "kind": kind, "episode": episode,
            "total": int(total[1]) if total else None, "tmdb_id": int(tmdb_id[1]) if tmdb_id else None,
            "quality": resolution(rest), "dv": bool(DOLBY_VISION.search(rest)), "cam": bool(CAM.search(rest))}


def timestamp(value, zone=timezone.utc):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=zone)).timestamp()


def best(*qualities):
    return max(qualities, key=lambda quality: QUALITY_ORDER.get(quality or "", 0))


def entry(key, **values):
    return {"key": key, "title": None, "year": None, "kind": None, "media": None, "imdb": None, "tmdb_id": None,
            "time": 0, "magnets": set(), "shares": set(), "quality": None, "dv": False, "episode": None,
            "total": None, "rating": None, "real": False, "anime": False, "curated": False, **values}


def merge(target, source):
    target["magnets"] |= source["magnets"]
    target["shares"] |= source["shares"]
    target["time"] = max(target["time"], source["time"])
    target["quality"] = best(target["quality"], source["quality"])
    for key in ("dv", "real", "anime", "curated"):
        target[key] = target[key] or source[key]
    for key in ("episode", "total"):
        target[key] = max((value for value in (target[key], source[key]) if value), default=None)
    for key in ("rating", "tmdb_id", "imdb", "media", "title", "year"):
        target[key] = target[key] or source[key]


def telegram_entries(messages, since):
    """One entry per titled post group; each 115 share or magnet in a post counts once."""
    entries = {}
    for message in messages:
        published, post = timestamp(message["published"]), parse_post(message["body"])
        if not post or not message["resources"] or not published or published < since:
            continue
        key = f"tg:{normalize_title(base_title(post['title']))}:{post['year']}"
        found = entry(key, title=post["title"], year=post["year"], kind=post["kind"], tmdb_id=post["tmdb_id"],
                      time=published, quality=post["quality"], dv=post["dv"], real=not post["cam"],
                      episode=post["episode"], total=post["total"], curated=True)
        for resource in message["resources"]:
            (found["shares"] if resource["kind"] == "share" else found["magnets"]).add(resource["key"])
        if key in entries:
            # An episode post makes the whole group a series.
            kind = "tv" if "tv" in (entries[key]["kind"], post["kind"]) else entries[key]["kind"] or post["kind"]
            merge(entries[key], found)
            entries[key]["kind"] = kind
        else:
            entries[key] = found
    return entries


def release_entries(rows):
    """One entry per title of the torrents recorded from YTS, EZTV and Mikan."""
    entries = {}
    for row in rows:
        episode = row.get("episode")
        found = entry(row["item"], title=row.get("title"), year=row.get("year"), kind=row.get("kind"),
                      imdb=row["item"].removeprefix("imdb:") if row["item"].startswith("imdb:") else None,
                      time=row["published"], magnets={row["hash"]}, quality=row.get("quality"),
                      episode=(row.get("season") or 1, episode) if episode else None, rating=row.get("rating"),
                      real=True, anime=row["item"].startswith("mikan:"))
        if row["item"] in entries:
            merge(entries[row["item"]], found)
        else:
            entries[row["item"]] = found
    return entries


def worth_linking(found, year):
    """Cheap checks before any TMDB lookup, so obscure torrents and old reposts do not use up the budget."""
    if not (found["magnets"] or found["shares"]):
        return False
    if found["imdb"] and found["kind"] == "movie":
        # YTS lists the IMDb rating, the only popularity hint available before linking.
        return (found["rating"] or 0) >= MIN_RATING
    if found["imdb"]:
        # Episodes of watched shows come in several releases; one-off uploads are mostly niche.
        return len(found["magnets"]) >= 2
    return found["anime"] or not found["year"] or found["year"] > year - RECENT_YEARS or bool(found["episode"])


def votes_label(votes):
    # Show an audience count only when there are enough TMDB votes to be meaningful.
    return (f"{votes / 10000:.1f}万" if votes >= 10000 else str(votes)) if votes >= ESTABLISHED_VOTES else None


def status(found):
    if found["total"]:
        return f"全{found['total']}集"
    if found["episode"]:
        season, number = found["episode"]
        if not number:
            return f"第{season}季"
        return f"更新至第{number}集" if season <= 1 else f"第{season}季第{number}集"
    return None


def day(moment):
    return datetime.fromtimestamp(moment, MIKAN_TIME).date()


def rank(found):
    return day(found["time"]), bool(found["shares"]), found["time"]


def card(found, media, detail, now):
    """The homepage card of a linked title, or None when it is filtered out."""
    if not found["real"] or not (found["magnets"] or found["shares"]):
        return None
    this_year = day(now).year
    aired = detail.get("last_air_date") if media["type"] == "tv" else None
    year = int(aired[:4]) if aired and aired[:4].isdigit() else detail.get("year")
    if not year or year <= this_year - RECENT_YEARS:
        return None
    rating, votes = detail.get("rating") or 0, detail.get("votes") or 0
    # New Chinese series often have only a handful of TMDB votes; their score is not meaningful yet.
    if votes >= RATED_VOTES and rating < MIN_RATING or HIDDEN_GENRES & set(detail.get("genre_ids") or []):
        return None
    if (detail.get("popularity") or 0) < (CURATED_POPULARITY if found["curated"] else OPEN_POPULARITY):
        return None
    if (not found["curated"] and media["type"] == "tv" and votes < ESTABLISHED_VOTES
            and (detail.get("year") or 0) <= this_year - RECENT_YEARS):
        return None
    countries = detail.get("countries") or []
    anime = found["anime"] or (ANIMATION in (detail.get("genre_ids") or [])
                               and (detail.get("original_language") in ANIME_LANGUAGES
                                    or bool(ANIME_ORIGINS & set(countries))))
    quality = best(found["quality"])
    original = detail.get("original_title")
    ratings = {"tmdb": round(rating, 1)} if rating and votes else {}
    if found["rating"]:
        ratings["imdb"] = round(found["rating"], 1)
    return {
        "id": f"{media['type']}/{media['id']}", "detail": f"https://www.themoviedb.org/{media['type']}/{media['id']}",
        "kind": "anime" if anime else media["type"], "title": detail["title"],
        "english_title": detail.get("english_title") or (original if original != detail["title"] else None),
        "year": year, "tags": ([COUNTRIES[countries[0]]] if countries and countries[0] in COUNTRIES else [])
        + [GENRES.get(genre, genre) for genre in (detail.get("genres") or [])[:3]],
        "poster": detail.get("poster"), "backdrop": detail.get("backdrop"),
        "overview": (detail.get("overview") or "")[:600],
        "counts": {"magnet": len(found["magnets"]), "cloud": len(found["shares"]), "online": 0},
        "qualities": [label for label in ("4K" if quality == "2160P" else quality, "DV" if found["dv"] else None)
                      if label],
        "ratings": ratings, "heat": votes_label(votes), "status": status(found) if media["type"] == "tv" else None,
        "media": media, "updated": found["time"],
    }


class LocalHome:
    def __init__(self, store, tmdb, yts, eztv, anime):
        self.store, self.tmdb, self.yts, self.eztv, self.anime = store, tmdb, yts, eztv, anime

    async def mikan_rows(self, items, since, errors):
        """Recent releases of the most recently updated shows, read from their Mikan feeds."""
        rows, mikan = [], self.anime.mikan
        for item in sorted(items, key=lambda item: item["updated"], reverse=True)[:SECTION_SIZE + 4]:
            key = f"home:mikan:{mikan.base()}:{item['id']}"
            cached = await blocking(self.store.cached_metadata, key)
            if cached and cached["expires"] > time.time() and cached["data"].get("updated") == item["updated"]:
                continue
            try:
                releases = await mikan.feed(item["id"])
            except Problem as exc:
                errors.append(exc.message)
                break
            for release in releases:
                published = timestamp(release["published"])
                if published and published >= since:
                    rows.append({"hash": release["hash"], "item": f"mikan:{item['id']}", "published": published,
                                 "source": "mikan", "title": release["title"], "kind": "anime",
                                 "quality": release_resolution(release["title"]),
                                 "episode": release_episode(release["title"])})
            await blocking(self.store.cache_metadata, key, {"updated": item["updated"]}, MIKAN_FEED_TTL)
        return rows

    async def search(self, found):
        """Link a channel title by name; a 🎬 post may still be a series, so films fall back to either kind."""
        item = {"title": found["title"], "year": found["year"], "kind": found["kind"] or "any"}
        key = f"home:tmdb:{normalize_title(base_title(found['title']))}:{found['year']}:"
        media = await link_tmdb(self.store, self.tmdb, key + item["kind"], item, True)
        if not media and found["kind"] == "movie":
            media = await link_tmdb(self.store, self.tmdb, key + "any", {**item, "kind": "any"}, True)
        return media

    async def resolve(self, found):
        """TMDB link and details of an entry, or None when it cannot be linked."""
        try:
            media = found["media"] or (await self.tmdb.find_imdb(found["imdb"]) if found["imdb"] else None)
            if not media and found["tmdb_id"]:
                guess = {"type": "tv" if found["kind"] == "tv" else "movie", "id": found["tmdb_id"]}
                try:
                    detail = await self.tmdb.detail(guess["type"], guess["id"])
                except Problem as exc:
                    if exc.code != 404:
                        raise
                    detail = {}
                names = {normalize_title(detail.get(key)) for key in
                         ("title", "chinese_title", "english_title", "original_title") if detail.get(key)}
                # The tag does not say whether the ID is a film or a series; trust it only when the title agrees.
                if normalize_title(base_title(found["title"])) in names:
                    return guess, detail
            if not media and found["title"] and not found["imdb"]:
                media = await self.search(found)
            return (media, await self.tmdb.detail(media["type"], media["id"])) if media else None
        except Problem as exc:
            if exc.code == 404:
                return None
            raise

    async def home(self):
        now = time.time()
        since, errors, rows = now - WINDOW, [], []
        for feed in (self.yts, self.eztv):
            try:
                rows.extend(await feed.latest())
            except Problem as exc:
                errors.append(exc.message)
        anime = []
        try:
            schedule = await self.anime.schedule()
            anime = [item for weekday in schedule["days"] for item in weekday["items"] if item.get("media")
                     and item.get("updated") and (timestamp(item["updated"], MIKAN_TIME) or 0) >= since]
            rows.extend(await self.mikan_rows(anime, since, errors))
        except Problem as exc:
            errors.append(exc.message)
        await blocking(self.store.record_releases, rows, since)
        entries = telegram_entries(await blocking(self.store.recent_messages), since)
        entries.update(release_entries(await blocking(self.store.recent_releases, since)))
        for item in anime:
            key = f"mikan:{item['id']}"
            show = entry(key, title=item["title"], kind="anime", media=item["media"], real=True, anime=True,
                         time=timestamp(item["updated"], MIKAN_TIME))
            if key in entries:
                merge(entries[key], show)
            else:
                entries[key] = show
        groups, full = {}, Counter()
        candidates = [found for found in entries.values() if worth_linking(found, day(now).year)]
        for found in sorted(candidates, key=rank, reverse=True)[:MAX_LINKED]:
            if all(full[key] >= SECTION_SIZE for key, _ in SECTIONS):
                break
            resolved = await self.resolve(found)
            if not resolved:
                continue
            media, detail = resolved
            group = (media["type"], media["id"])
            if group in groups:
                merge(groups[group][0], found)
                continue
            groups[group] = (found, media, detail)
            shown = card(found, media, detail, now)
            if shown:
                full[shown["kind"]] += 1
        cards, popularity = [], {}
        for found, media, detail in sorted(groups.values(), key=lambda group: rank(group[0]), reverse=True):
            shown = card(found, media, detail, now)
            if shown:
                cards.append(shown)
                popularity[shown["id"]] = detail.get("popularity") or 0
        if not cards and errors:
            raise Problem("；".join(dict.fromkeys(errors)), 502)
        sections = [{"key": key, "title": title, "items": [c for c in cards if c["kind"] == key][:SECTION_SIZE]}
                    for key, title in SECTIONS]
        # New films lead; series fill in when there are fewer than three.
        featured = sorted((c for c in cards if c["backdrop"] and c["updated"] >= now - FEATURED_WINDOW),
                          key=lambda shown: (shown["kind"] == "movie", popularity[shown["id"]]), reverse=True)[:3]
        return {"featured": featured, "sections": [s for s in sections if s["items"]],
                "errors": list(dict.fromkeys(errors)), "origin": "local"}
