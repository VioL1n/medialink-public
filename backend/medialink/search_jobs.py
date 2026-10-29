import asyncio
import re
import time
import unicodedata
from contextlib import aclosing

from .core import Problem
from .sources import merge_results
from .tmdb import media_queries, normalize_title, query_title


def title_matches(title, names):
    title = unicodedata.normalize("NFKC", title).casefold()
    for name in names:
        if not name:
            continue
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", name):
            if len(normalize_title(name)) >= 2 and normalize_title(name) in normalize_title(title):
                return True
        else:
            words = re.findall(r"\w+", unicodedata.normalize("NFKC", name).casefold())
            if words and re.search(r"(?<!\w)" + r"[\W_]+".join(re.escape(w) for w in words) + r"(?!\w)", title):
                return True
    return False


QUALITY_ORDER = {"8K": 6, "4K": 5, "2160P": 5, "1080P": 4, "1080I": 3, "720P": 2, "480P": 1}


def sort_results(results):
    return sorted(
        results,
        key=lambda r: (
            QUALITY_ORDER.get((r.get("resolution") or "").strip().upper(), 0),
            (r.get("match") or {}).get("score", 0) or 0,
        ),
        reverse=True,
    )


def rank_results(results, media=None, season_number=None):
    if not media:
        return sort_results(results)
    ranked = []
    names = [media.get(k) for k in ("title", "chinese_title", "english_title", "original_title")]
    for resource in results:
        title = resource["title"]
        matches = title_matches(title, names)
        release_text = unicodedata.normalize("NFKC", title).casefold()
        for name in names:
            if name:
                words = re.findall(r"\w+", unicodedata.normalize("NFKC", name).casefold())
                if words:
                    release_text = re.sub(r"[\W_]*".join(re.escape(word) for word in words), "", release_text)
        years = {int(y) for y in re.findall(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", release_text)}
        year = media.get("year")
        score, reasons, conflict = (40, ["片名匹配"], False) if matches else (0, ["片名需核对"], False)
        if year:
            if year in years:
                score += 20
                reasons.append("年份匹配")
            elif years:
                score -= 40
                conflict = True
                reasons.append("年份不同")
            else:
                reasons.append("年份未知")
        season_match = False
        if season_number is not None:
            seasons = {int(s) for s in re.findall(r"\bS(\d{1,3})(?:E\d+)?\b", title, re.I)}
            for word in re.findall(r"第([\d零一二三四五六七八九十]+)季", title):
                if word.isdigit():
                    seasons.add(int(word))
                elif "十" in word:
                    left, right = word.split("十", 1)
                    digits = "零一二三四五六七八九"
                    if (not left or left in digits) and (not right or right in digits):
                        seasons.add((digits.index(left) if left else 1) * 10 + (digits.index(right) if right else 0))
                elif word in "零一二三四五六七八九":
                    seasons.add("零一二三四五六七八九".index(word))
            season_match = season_number in seasons
            if season_match:
                score += 30
                reasons.append("季数匹配")
            elif seasons:
                score -= 60
                conflict = True
                reasons.append("季数不同")
            else:
                reasons.append("季数未知")
        strong = (
            matches
            and not conflict
            and (
                season_match
                if season_number is not None
                else media["type"] == "tv" or year is not None and year in years
            )
        )
        ranked.append(
            {
                **resource,
                "match": {
                    "status": "matched" if strong else "review",
                    "score": score,
                    "explanation": " · ".join(reasons),
                },
            }
        )
    return sort_results(ranked)


class SearchEngine:
    source_budget = 60
    metadata_budget = 8

    def __init__(self, sources, tmdb):
        self.sources, self.tmdb = sources, tmdb

    async def resolve(self, request):
        query = request.query.strip()
        fallback = {"status": "disabled", "queries": [query], "media": None, "candidates": [], "season_number": None}
        if not request.use_tmdb and not request.media:
            return fallback
        state = await self.tmdb.state()
        if not state["tmdb_configured"]:
            return {**fallback, "status": "unconfigured", "message": "TMDB 尚未配置，按原关键词搜索"}
        try:
            async with asyncio.timeout(self.metadata_budget):
                season = request.media.season_number if request.media else None
                if request.media:
                    media = await self.tmdb.detail(request.media.type, request.media.id)
                    candidates = []
                    if season is not None and not any(s["number"] == season for s in media.get("seasons", [])):
                        raise Problem("这一季不在 TMDB 的剧集资料中", 422)
                else:
                    title, year = query_title(query)
                    response = await self.tmdb.search(title)
                    candidates = response["items"]
                    exact = [
                        c
                        for c in candidates
                        if (year is None or c.get("year") == year)
                        and any(
                            normalize_title(c.get(k)) == normalize_title(title)
                            for k in ("title", "chinese_title", "english_title", "original_title")
                        )
                    ]
                    if len(exact) != 1 or response.get("total_pages", 1) > 1:
                        return {
                            **fallback,
                            "status": "candidates" if candidates else "no_match",
                            "candidates": candidates,
                            "message": "请选择要搜索的影片" if candidates else "TMDB 未找到匹配影片，按原关键词搜索",
                            "updated": response["updated"],
                            "stale": response["stale"],
                        }
                    media = await self.tmdb.detail(exact[0]["type"], exact[0]["id"])
                return {
                    "status": "resolved",
                    "media": media,
                    "candidates": candidates,
                    "queries": media_queries(query, media, season),
                    "season_number": season,
                    "updated": media["updated"],
                    "stale": media["stale"],
                }
        except (Problem, TimeoutError) as exc:
            return {
                **fallback,
                "status": "error",
                "message": exc.message if isinstance(exc, Problem) else "TMDB 识别超时，按原关键词搜索",
            }
        except Exception:
            return {**fallback, "status": "error", "message": "TMDB 资料暂不可用，按原关键词搜索"}

    async def execute(self, request, job):
        deadline = time.monotonic() + self.source_budget
        metadata_task = asyncio.create_task(self.resolve(request))
        collected = {}

        def ranked():
            metadata = job.get("metadata", {})
            return rank_results(
                merge_results([r for batch in collected.values() for r in batch]),
                metadata.get("media"),
                metadata.get("season_number"),
            )

        async def metadata():
            result = await metadata_task
            job["metadata"] = result
            job["events"].append({"_event": "metadata", **result})
            job["results"] = ranked()

        announcement = asyncio.create_task(metadata())

        async def run_source(name):
            collected[name] = []
            errors, successes, seen = [], 0, set()

            async def run(query):
                nonlocal successes
                seen.add(normalize_title(query))

                def receive(batch):
                    nonlocal successes
                    successes += 1
                    collected[name] = merge_results(
                        collected[name] + [{**r, "matched_queries": [query]} for r in batch]
                    )
                    job["results"] = ranked()
                    job["events"].append(
                        {
                            "provider": name,
                            "results": rank_results(
                                collected[name],
                                job.get("metadata", {}).get("media"),
                                job.get("metadata", {}).get("season_number"),
                            ),
                            "status": "loading",
                        }
                    )

                try:
                    source = self.sources[name]
                    if callable(getattr(type(source), "iter_search", None)):
                        async with aclosing(source.iter_search(query)) as batches:
                            async for batch in batches:
                                receive(batch)
                    else:
                        receive(await source.search(query))
                except Problem as exc:
                    errors.append(exc.message)
                    if getattr(exc, "stop_source", False) or "人机验证" in exc.message:
                        return False
                except TimeoutError:
                    errors.append("该来源搜索超时")
                except Exception:
                    errors.append("该来源暂不可用")
                return True

            try:
                async with asyncio.timeout_at(deadline):
                    proceed = True
                    if not request.media:
                        proceed = await run(request.query.strip())
                    resolved = await asyncio.shield(metadata_task)
                    if proceed:
                        for query in resolved["queries"]:
                            if normalize_title(query) not in seen and not await run(query):
                                break
            except TimeoutError:
                errors.append("该来源搜索超时，已取得的结果保留")
            job["results"] = ranked()
            event = {
                "provider": name,
                "results": rank_results(
                    collected[name], job.get("metadata", {}).get("media"), job.get("metadata", {}).get("season_number")
                ),
                "status": "done" if successes else "error",
            }
            if errors:
                event["error"] = "；".join(dict.fromkeys(errors))
            job["events"].append(event)

        try:
            await asyncio.gather(*(run_source(name) for name in dict.fromkeys(request.sources)), announcement)
            job["results"] = ranked()
            job["done"] = True
        finally:
            metadata_task.cancel()
            announcement.cancel()
