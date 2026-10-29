"""Deterministic ingestion naming plans; no remote writes occur here."""
import copy
import hashlib
import re
import unicodedata
from pathlib import PurePosixPath

from .core import Problem
from .curation import PROMOTIONAL_NOTE, ROLE_LABELS, SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS, classify, kind_of

RESOLUTIONS = {"480P", "576P", "720P", "1080P", "1080I", "2160P", "4320P"}
QUALITY_RE = re.compile(r"(?<![a-z0-9])(4320p|2160p|1080[pi]|720p|576p|480p|4k|8k)(?![a-z0-9])", re.I)
NUMBER = r"[0-9零〇一二两三四五六七八九十百千]+"
MISSING_EPISODE = "缺少明确季集号，请补全"
MISSING_QUALITY = "缺少清晰度，请补全"
# Release-group episode markers such as "[05]", "[02v2]" and " - 11 ", used only when nothing explicit exists.
LOOSE_EPISODE = (
    re.compile(r"[\[【(（]\s*(\d{1,4})(?:v\d{1,2})?\s*(?:end|完)?\s*[\]】)）]", re.I),
    re.compile(r"(?:^|\s)-\s*(\d{1,4})(?:v\d{1,2})?(?=\s|$|[\[【(（.])", re.I),
    re.compile(r"(?<![a-z0-9])episode[ ._-]*(\d{1,4})(?!\d)", re.I),
    re.compile(r"#(\d{1,4})(?!\d)"),
    re.compile(rf"第({NUMBER})回"),
)
EXPLICIT_EPISODE = re.compile(r"(?<![a-z0-9])s\d{1,4}[ ._-]*e\d{1,4}(?!\d)|(?<![a-z0-9])\d{1,2}x\d{1,4}(?!\d)", re.I)
# OVA/OAD/SP numbering belongs to TMDB season 0.
SPECIAL_EPISODE = re.compile(r"(?<![a-z0-9])(?:ova|oad|sp|special)[ ._-]*(\d{1,3})(?!\d)", re.I)
LANGUAGE_TAGS = {"chs", "cht", "sc", "tc", "gb", "big5", "zh", "zh-cn", "zh-tw", "zh-hk", "zh-hans", "zh-hant", "chi",
                 "zho", "eng", "en", "jpn", "ja", "jp", "kor", "ko", "简体", "繁体", "简中", "繁中", "简繁", "简日", "繁日",
                 "简英", "繁英", "中英", "双语", "中文", "简体中文", "繁体中文", "forced", "sdh", "default"}
NOT_EPISODE = {480, 576, 720, 1080, 2160, 4320}


def normalize_resolution(value):
    quality = (value or "").strip().upper()
    return {"4K": "2160P", "8K": "4320P"}.get(quality, quality) if quality else None


def file_id(path):
    return hashlib.sha256(path.encode()).hexdigest()[:24]


def safe_title(value):
    value = "".join(c for c in unicodedata.normalize("NFC", value or "") if not unicodedata.category(c).startswith("C"))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', " ", value)
    return re.sub(r"\s+", " ", value).strip(" .")


def chinese_number(value):
    if value.isdecimal():
        return int(value)
    digits = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    total = number = 0
    for char in value:
        if char in digits:
            number = digits[char]
        else:
            unit = {"十": 10, "百": 100, "千": 1000}[char]
            total += (number or 1) * unit
            number = 0
    return total + number


def season_numbers(text):
    values = [int(m[1]) for m in re.finditer(r"(?<![a-z0-9])s(\d{1,4})(?!\d)", text, re.I)]
    # "Season - 172" is an episode after a season label, not season 172.
    values += [int(m[1]) for m in re.finditer(r"(?<![a-z0-9])season(?:[ ._]*|-)(\d{1,4})(?!\d)", text, re.I)]
    values += [int(m[1]) for m in re.finditer(r"(?<![a-z0-9])(\d{1,2})(?:st|nd|rd|th)[ ._-]*season(?![a-z])", text, re.I)]
    values += [chinese_number(m[1]) for m in re.finditer(rf"第?({NUMBER})季", text)]
    if re.search(r"特别篇|特別篇|specials", text, re.I):
        values.append(0)
    return set(values)


def loose_episode(stem):
    values = set()
    for pattern in LOOSE_EPISODE:
        for m in pattern.finditer(stem):
            value = chinese_number(m[1])
            if not value or value in NOT_EPISODE or (len(m[1]) == 4 and 1900 <= value <= 2099):
                continue
            values.add(value)
    return values.pop() if len(values) == 1 else None


def episode_info(path, selected_season=None):
    stem = PurePosixPath(path).stem
    pairs = [(int(m[1]), int(m[2])) for m in re.finditer(r"(?<![a-z0-9])s(\d{1,4})[ ._-]*e(\d{1,4})(?!\d)", stem, re.I)]
    # One- or two-digit seasons only, so "1920x1080" is never read as season 1920.
    pairs += [(int(m[1]), int(m[2])) for m in re.finditer(r"(?<![a-z0-9])(\d{1,2})x(\d{1,4})(?!\d)", stem, re.I)]
    episodes = {e for _, e in pairs}
    episodes.update(int(m[1]) for m in re.finditer(r"(?<![a-z0-9])e(?:p)?[ ._-]*(\d{1,4})(?!\d)", stem, re.I))
    episodes.update(chinese_number(m[1]) for m in re.finditer(rf"第?({NUMBER})[集话話]", stem))
    merged = bool(re.search(
        rf"(?:e\d{{1,4}}[ ._-]*e\d|e\d{{1,4}}\s*[-~至到+]\s*(?:e)?\d{{1,4}}(?![a-z0-9])|(?<![a-z0-9])\d{{1,2}}x\d+\s*[-~+]\s*\d{{1,4}}(?![a-z0-9])|第?{NUMBER}[集话話]?\s*[-~至到+]\s*第?{NUMBER}[集话話])",
        stem, re.I,
    )) or len(pairs) > 1 or len(episodes) > 1
    if merged:
        return None, None, "多集合并视频需人工处理，不能填写为单集", True
    if not episodes and (special := SPECIAL_EPISODE.search(stem)) and int(special[1]) >= 1:
        return 0, int(special[1]), None, False
    if not episodes and (loose := loose_episode(stem)) is not None:
        episodes = {loose}
    seasons = season_numbers(stem) | {s for s, _ in pairs}
    parent_seasons = next((season_numbers(parent.name) for parent in PurePosixPath(path).parents
                           if parent.name and season_numbers(parent.name)), set())
    seasons |= parent_seasons
    if selected_season is not None:
        seasons.add(selected_season)
    if len(seasons) > 1:
        return None, None, "文件、目录或所选季数存在冲突", False
    season = next(iter(seasons), None)
    episode = next(iter(episodes), None)
    if (season is not None and season > 1000) or (episode is not None and not 1 <= episode <= 9999):
        return season, episode, "季集号超出支持范围", False
    if season is None or episode is None:
        return season, episode, MISSING_EPISODE, False
    return season, episode, None, False


def quality_info(path, resource_resolution, override):
    if override:
        return normalize_resolution(override), "人工填写", None
    p = PurePosixPath(path)
    for label, text in [("文件名", p.stem), *(("目录名", parent.name) for parent in p.parents if parent.name)]:
        values = {normalize_resolution(m[1]) for m in QUALITY_RE.finditer(text)}
        if len(values) > 1:
            return None, label, "清晰度标注存在冲突，请修正"
        if values:
            return values.pop(), label, None
    quality = normalize_resolution(resource_resolution)
    return quality, "资源标注" if quality else "", None if quality else MISSING_QUALITY


def media_snapshot(media):
    snapshot = {key: media.get(key) for key in ("id", "type", "title", "chinese_title", "original_title", "year")}
    if media.get("type") == "tv" and media.get("seasons"):
        # Episode counts let library plans reject impossible numbers and map absolute numbering.
        snapshot["seasons"] = {str(s["number"]): s.get("episodes") for s in media["seasons"]
                               if type(s.get("number")) is int}
    return snapshot


def library_folder(media):
    """Emby title folder; the TMDB id makes Emby's identification exact."""
    name = safe_title(media.get("chinese_title") or media.get("original_title") or media.get("title"))
    if not name or not media.get("id"):
        return None
    year = f" ({media['year']})" if media.get("year") else ""
    return f"{name}{year} [tmdbid={media['id']}]"


def tmdb_seasons(media):
    seasons = media.get("seasons")
    if not isinstance(seasons, dict):
        return None
    return {int(number): count for number, count in seasons.items() if str(number).isdecimal()}


def check_episode(seasons, season, episode, explicit):
    """Validate against TMDB episode counts, mapping absolute numbering; return (episode, note, error)."""
    if not seasons or season is None or episode is None:
        return episode, None, None
    if season not in seasons:
        return episode, None, f"TMDB 没有第 {season} 季，请核对季号"
    count = seasons[season]
    if type(count) is not int or count <= 0 or episode <= count:
        return episode, None, None
    previous = [seasons.get(number) for number in range(1, season)]
    if not explicit and season > 0 and all(type(c) is int and c > 0 for c in previous):
        offset = sum(previous)
        if offset < episode <= offset + count:
            return episode - offset, "按 TMDB 绝对集号换算", None
    return episode, None, f"第 {season} 季在 TMDB 只有 {count} 集，请核对集号"


def language_tail(stem):
    tags = [part for part in re.split(r"[._\s\[\]()（）【】]+", stem) if part.lower() in LANGUAGE_TAGS]
    return "".join("." + tag for tag in dict.fromkeys(tags))


def file_choices(naming):
    """Curated keep/exclude decisions; explicit user choices take precedence over the rules."""
    files = naming.get("files", [])
    inclusions = naming.get("inclusions", {})
    selected = naming.get("selected_paths")
    selected = None if selected is None else set(selected)
    choices = classify(files)
    for f in files:
        choice = choices[f["path"]]
        if choice["role"] == "other":
            continue
        identifier = file_id(f["path"])
        if identifier in inclusions:
            choice.update(keep=bool(inclusions[identifier]),
                          reason="已手动选择入库" if inclusions[identifier] else "已手动排除")
        elif selected is not None and (f["path"] in selected) != choice["keep"]:
            choice.update(keep=f["path"] in selected, reason="下载前已勾选" if f["path"] in selected else "下载前未勾选")
    return choices


def excluded_entry(f, role, reason):
    return {"id": file_id(f["path"]), "source": f["path"], "size": f["size"], "kind": kind_of(f["path"]),
            "role": role, "label": ROLE_LABELS[role], "reason": reason}


def subtitle_videos(path, videos, media, naming):
    if media.get("type") == "movie":
        return videos if len(videos) == 1 else []
    season, episode, _, merged = episode_info(path, naming.get("season_number"))
    if merged or episode is None:
        return []
    return [video for video in videos if video.get("episode_number") == episode
            and (season is None or video.get("season_number") == season)]


def name_key(path):
    # Case folding and NFC avoid unsafe collisions across mounted filesystems.
    return unicodedata.normalize("NFC", path).casefold()


def build_plan(naming):
    media = naming.get("media") or {}
    # Version 2 plans curate the download and lay files out as an Emby title folder.
    library = naming.get("version") == 2
    title = safe_title(media.get("original_title") if media.get("type") == "movie"
                       else media.get("chinese_title") or media.get("original_title"))
    files = naming.get("files", [])
    overrides = naming.get("overrides", {})
    suggestions = naming.get("suggestions", {})
    plan, warnings, excluded = [], [], []
    global_error = None if media.get("id") and media.get("type") in {"movie", "tv"} and title else "请关联具有有效片名的 TMDB 电影或剧集"
    folder = library_folder(media) if library else None
    if library and not global_error and len(folder.encode()) > 255:
        global_error = "标题文件夹名称超过 255 字节，请人工处理"
    choices = file_choices(naming) if library else None
    seasons = tmdb_seasons(media) if library and media.get("type") == "tv" else None
    regular = sorted(number for number in seasons or {} if number > 0)
    for f in files:
        p = PurePosixPath(f["path"])
        if p.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if choices:
            choice = choices[f["path"]]
            if not choice["keep"]:
                excluded.append(excluded_entry(f, choice["role"], choice["reason"]))
                continue
            if choice["reason"] == PROMOTIONAL_NOTE:
                warnings.append(f"{f['path']}：{PROMOTIONAL_NOTE}")
        identifier = file_id(f["path"])
        override = overrides.get(identifier, {})
        suggestion = suggestions.get(identifier, {})
        context = naming.get("preview_context", {}).get(identifier, {})
        item = {"id": identifier, "source": f["path"], "target": None, "kind": "video",
                "size": f["size"], "identity": f.get("identity", {}), "status": "pending", "error": global_error,
                "missing_fields": []}
        if media.get("type") == "movie":
            quality, basis, error = quality_info(f["path"], naming.get("resolution"), override.get("resolution"))
            if not global_error and context.get("resolution") in RESOLUTIONS and (error == MISSING_QUALITY or basis == "资源标注"):
                quality, basis, error = context["resolution"], "已确认预览的目录信息", None
            if error == MISSING_QUALITY and not global_error:
                if suggestion.get("resolution") in RESOLUTIONS:
                    quality, basis, error = suggestion["resolution"], "大模型补全", None
                    item["llm_reason"] = suggestion.get("reason", "")
                else:
                    item["missing_fields"] = ["resolution"]
            item.update(resolution=quality, basis=basis, error=global_error or error)
            if not library:
                stem = f"{title} {quality}"
            else:
                stem = f"{title} ({media['year']}) - {quality}" if media.get("year") else f"{title} - {quality}"
        else:
            season, episode, error, merged = episode_info(f["path"], naming.get("season_number"))
            single = library and error == MISSING_EPISODE and season is None and len(regular) == 1
            if single:
                season = regular[0]
                error = None if episode is not None else MISSING_EPISODE
            can_suggest = error in {None, MISSING_EPISODE} and not merged and not global_error
            manual = not merged and (override.get("season_number") is not None or override.get("episode_number") is not None)
            if manual:
                season = override.get("season_number", season)
                episode = override.get("episode_number", episode)
                error = None if season is not None and episode is not None and 0 <= season <= 1000 and 1 <= episode <= 9999 else "缺少有效季集号，请补全"
                basis = "人工填写"
            else:
                basis = "文件名／目录／所选季数" + (" + TMDB 唯一正片季" if single else "")
            if can_suggest:
                used = []
                for key, value in (("season_number", season), ("episode_number", episode)):
                    if value is None:
                        inferred = context.get(key, suggestion.get(key))
                        limit = (0, 1000) if key == "season_number" else (1, 9999)
                        if type(inferred) is int and limit[0] <= inferred <= limit[1]:
                            if key == "season_number":
                                season = inferred
                            else:
                                episode = inferred
                            used.append("preview" if key in context else key)
                        else:
                            item["missing_fields"].append(key)
                if used:
                    if "preview" in used:
                        basis += " + 已确认预览的目录信息"
                    if any(key != "preview" for key in used):
                        basis += " + 大模型补全"
                        item["llm_reason"] = suggestion.get("reason", "")
                error = None if season is not None and episode is not None else MISSING_EPISODE
            if library and not manual and error is None and not global_error:
                # Manual values are trusted; TMDB may lag behind newly aired episodes.
                episode, note, error = check_episode(seasons, season, episode, bool(EXPLICIT_EPISODE.search(p.stem)))
                if note:
                    basis += " + " + note
            item.update(season_number=season, episode_number=episode, basis=basis, error=global_error or error)
            stem = f"{title} S{season:02d}E{episode:02d}" if season is not None and episode is not None else ""
        if not item["error"]:
            name = stem + p.suffix
            if len(name.encode()) > 255:
                item["error"] = "规范文件名超过 255 字节，请人工处理"
            else:
                item["target"] = str(p.with_name(name))
                if library:
                    season_folder = [f"Season {item['season_number']:02d}"] if media.get("type") == "tv" else []
                    item["destination"] = str(PurePosixPath(folder, *season_folder, name))
        plan.append(item)
    videos = list(plan)
    video_stems = {}
    for video in videos:
        vp = PurePosixPath(video["source"])
        video_stems.setdefault((str(vp.parent), vp.stem), []).append(video)
    for f in files:
        p = PurePosixPath(f["path"])
        if p.suffix.lower() not in SUBTITLE_EXTENSIONS:
            continue
        if choices and not choices[f["path"]]["keep"]:
            excluded.append(excluded_entry(f, "subtitle", choices[f["path"]]["reason"]))
            continue
        matches = []
        # Lookup only exact stems and dot-delimited prefixes, not every video in a season pack.
        prefixes = [p.stem] + [p.stem[:m.start()] for m in re.finditer(r"\.", p.stem)]
        for prefix in prefixes:
            tail = p.stem[len(prefix):]
            if re.fullmatch(r"(?:\.(?:[a-z]{2,3}(?:-[a-z]{2,4})?|chs|cht|简体|繁体|简中|繁中|双语|forced|default|sdh|cc|hearing_impaired))*", tail, re.I):
                matches.extend((video, tail) for video in video_stems.get((str(p.parent), prefix), []))
        if library and len(matches) != 1:
            matches = [(video, language_tail(p.stem)) for video in subtitle_videos(f["path"], videos, media, naming)]
        if len(matches) != 1:
            if library:
                excluded.append(excluded_entry(f, "subtitle", "字幕无法对应入库视频，保留在暂存目录"))
            else:
                warnings.append(f"{f['path']}：字幕对应关系不明确，保留原名")
            continue
        video, tail = matches[0]
        target = str(p.with_name(PurePosixPath(video["target"]).stem + tail + p.suffix)) if video["target"] else None
        entry = {"id": file_id(f["path"]), "source": f["path"], "target": target, "kind": "subtitle",
                 "size": f["size"], "identity": f.get("identity", {}), "status": "pending",
                 "basis": "对应视频", "error": video["error"]}
        if library and video.get("destination"):
            placed = PurePosixPath(video["destination"])
            entry["destination"] = str(placed.with_name(placed.stem + tail + p.suffix))
            if any(name_key(entry["destination"]) == name_key(item.get("destination") or "") for item in plan):
                excluded.append(excluded_entry(f, "subtitle", "与其他字幕规范名称相同，保留在暂存目录"))
                continue
        plan.append(entry)
    if choices:
        excluded += [excluded_entry(f, "other", choices[f["path"]]["reason"]) for f in files
                     if choices[f["path"]]["role"] == "other"]
    # Compare final names against every file and directory, including unchanged attachments.
    by_source = {item["source"]: item for item in plan}
    destinations = {}
    directories = {str(parent) for f in files for parent in PurePosixPath(f["path"]).parents if str(parent) != "."}
    directories.update(naming.get("directories", []))
    for f in files:
        item = by_source.get(f["path"])
        destination = item["target"] if item and item["target"] else f["path"]
        destinations.setdefault(name_key(destination), []).append(item)
    directory_keys = {name_key(d) for d in directories}
    for key, items in destinations.items():
        if len(items) > 1 or key in directory_keys:
            for item in items:
                if item:
                    item["error"] = "规范名称与同目录内容冲突，请拆分资源或人工处理"
                    item["missing_fields"] = []
    # A rename may not target another source, even if that source would later move away.
    original_keys = {name_key(f["path"]): f["path"] for f in files}
    for item in plan:
        if item["target"]:
            occupied = original_keys.get(name_key(item["target"]))
            if occupied and occupied != item["source"]:
                item["error"] = "规范名称已被暂存文件占用，请人工处理"
                item["missing_fields"] = []
            if len(PurePosixPath(item["target"]).name.encode()) > 255:
                item["error"] = "规范文件名超过 255 字节，请人工处理"
    if library:
        # Files from different release folders end up side by side in the title folder.
        placed = {}
        for item in plan:
            if item.get("destination"):
                placed.setdefault(name_key(item["destination"]), []).append(item)
        for items in placed.values():
            if len(items) > 1:
                for item in items:
                    item["error"] = "多个文件将得到同一个规范名称，请排除重复版本或人工处理"
                    item["missing_fields"] = []
    errors = list(dict.fromkeys(item["error"] for item in plan if item["error"]))
    if not videos:
        errors.append("没有可入库的影视文件，请在任务详情选择要入库的文件" if library and files else "尚未获得影视文件清单")
    result = {**naming, "plan": plan, "warnings": warnings, "errors": errors}
    if library:
        result.update(excluded=excluded, folder=folder)
    return result


def model_candidates(naming):
    return [{"id": item["id"], "path": item["source"], "missing_fields": item["missing_fields"],
             **{key: item[key] for key in ("resolution", "season_number", "episode_number")
                if item.get(key) is not None}}
            for item in naming.get("plan", []) if item["kind"] == "video" and item.get("missing_fields")]


def apply_file_corrections(naming, corrections):
    videos = {item["id"] for item in naming.get("plan", []) if item["kind"] == "video"}
    choosable = set()
    if naming.get("version") == 2:
        # Library plans may bring an excluded file back, or exclude a curated one.
        choosable = {file_id(f["path"]) for f in naming.get("files", []) if kind_of(f["path"]) != "other"}
        videos |= {file_id(f["path"]) for f in naming.get("files", []) if kind_of(f["path"]) == "video"}
    if len({item.id for item in corrections}) != len(corrections):
        raise Problem("文件修正标识重复", 422)
    for correction in corrections:
        values = correction.model_dump(exclude_unset=True, exclude={"id"})
        if "included" in values:
            if correction.id not in choosable:
                raise Problem("文件标识无效，请刷新文件清单", 422)
            included = values.pop("included")
            inclusions = naming.setdefault("inclusions", {})
            if included is None:
                inclusions.pop(correction.id, None)
            else:
                inclusions[correction.id] = included
            if not values:
                continue
        if correction.id not in videos:
            raise Problem("文件标识无效，请刷新文件清单", 422)
        if "resolution" in values:
            values["resolution"] = normalize_resolution(values["resolution"])
            if values["resolution"] and values["resolution"] not in RESOLUTIONS:
                raise Problem("清晰度格式无效", 422)
        previous = naming.setdefault("overrides", {}).get(correction.id, {})
        naming["overrides"][correction.id] = {k: v for k, v in (previous | values).items() if v is not None}


def bind_confirmed_preview(naming):
    """Bind a trusted draft to downloaded files, allowing one omitted torrent root."""
    naming = copy.deepcopy(naming)
    preview = naming.get("preview")
    if not preview or not preview.get("confirmed"):
        return naming, True
    originals, actual = preview["files"], naming["files"]
    root = preview.get("root")
    mappings = []
    for strip in (False, True) if root else (False,):
        def mapped(path):
            prefix = root + "/" if strip else ""
            return path[len(prefix):] if prefix and path.startswith(prefix) else path

        paths = {f["path"]: mapped(f["path"]) for f in originals}
        expected = {paths[f["path"]]: f["size"] for f in originals}
        if (len(expected) == len(originals) == len(actual)
                and expected == {f["path"]: f["size"] for f in actual}
                and paths not in [item[0] for item in mappings]):
            mappings.append((paths, {paths[source]: mapped(target) for source, target in preview["targets"].items()},
                             {paths[source]: target for source, target in preview.get("destinations", {}).items()}))
    approved = len(mappings) == 1
    if approved:
        paths, targets, destinations = mappings[0]
        for field in ("overrides", "suggestions", "inclusions"):
            old = naming.get(field, {})
            naming[field] = {file_id(destination): old[file_id(source)] for source, destination in paths.items()
                             if file_id(source) in old}
        naming["preview_context"] = {file_id(paths[item["source"]]): item["values"]
                                     for item in preview.get("context", [])}
        naming = build_plan(naming)
        approved = (not naming["errors"] and targets == {item["source"]: item["target"] for item in naming["plan"]}
                    and destinations == {item["source"]: item["destination"] for item in naming["plan"]
                                         if item.get("destination")})
    else:
        # Stale IDs must not accidentally attach suggestions to a changed file set.
        naming.update(overrides={}, suggestions={}, inclusions={})
        naming = build_plan(naming)
    if approved:
        naming.update(review_required=False, confirmed_revision=naming.get("revision", 0))
    else:
        message = "下载文件或规范名称与已确认预览不一致，请在任务详情核对"
        naming["preview"]["confirmed"] = False
        naming.update(review_required=True, llm={"status": "superseded", "message": message})
        naming["errors"] = list(dict.fromkeys([*naming.get("errors", []), message]))
    return naming, approved


def public_naming(task, include_files=True):
    naming = task["payload"].get("naming")
    if not naming:
        return None
    editable = not naming.get("locked") and task["state"] not in {"completed", "canceled"}
    return {"version": naming["version"], "media": naming.get("media"),
            "season_number": naming.get("season_number"), "resolution": naming.get("resolution"),
            "editable": editable, "locked": bool(naming.get("locked")),
            "revision": naming.get("revision", 0), "review_required": bool(naming.get("review_required")),
            "llm": {key: naming.get("llm", {}).get(key) for key in ("status", "message")},
            "files": [{k: v for k, v in item.items() if k not in {"identity", "size", "attempted_at"}}
                      for item in naming.get("plan", [])] if include_files else [],
            "warnings": naming.get("warnings", []) if include_files else [], "errors": naming.get("errors", []),
            "folder": naming.get("folder"), "excluded_count": len(naming.get("excluded", [])),
            "excluded": naming.get("excluded", []) if include_files else []}
