import asyncio
import hashlib
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .access import host_allowed, migrate_public_access, normalize_public_hosts
from .anime import AnimeSchedule, BgmTv, Mikan, Subscriptions, normalize_rules
from .clients import P115, OpenList, RemoteFailure
from .core import DEFAULTS, SEARCH_SOURCE_IDS, Problem, Store, Vault, blocking, resource_key, target_path
from .home import HomeFeed
from .llm import validate_settings as validate_llm_settings
from .local_home import LocalHome
from .magnets import MagnetPreviews, selection_signature
from .naming import (
    RESOLUTIONS,
    apply_file_corrections,
    build_plan,
    library_folder,
    media_snapshot,
    normalize_resolution,
    public_naming,
)
from .naming_previews import NamingPreviews
from .search_jobs import SearchEngine
from .sources import Nyaa, Telegram
from .tmdb import TMDB
from .web_sources import Eztv, Knaben, LimeTorrents, TelegramWeb, Yts
from .worker import Worker


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


class Credentials(BaseModel):
    password: str = Field(min_length=8, max_length=200)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=8, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


class MediaSelection(BaseModel):
    type: Literal["movie", "tv"]
    id: int = Field(gt=0)
    season_number: int | None = Field(default=None, ge=0, le=1000)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    sources: list[str] | None = Field(default_factory=lambda: list(SEARCH_SOURCE_IDS), max_length=5)
    use_tmdb: bool | None = True
    media: MediaSelection | None = None


class NamingFileCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=24, max_length=24)
    resolution: str | None = Field(default=None, max_length=16)
    season_number: int | None = Field(default=None, ge=0, le=1000)
    episode_number: int | None = Field(default=None, ge=1, le=9999)
    included: bool | None = None


class NamingCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media: MediaSelection | None = None
    resolution: str | None = Field(default=None, max_length=16)
    files: list[NamingFileCorrection] = Field(default_factory=list, max_length=20000)


class NamingConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=0)


def checked_resolution(value):
    result = normalize_resolution(value)
    if result and result not in RESOLUTIONS:
        raise Problem("清晰度格式无效", 422)
    return result


class MagnetRequest(BaseModel):
    link: str = Field(max_length=8192)


class MagnetSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    selected_ids: list[str] = Field(max_length=20000)


class NamingPreviewReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    preview_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision: int = Field(ge=0)


class NamingPreviewCorrection(NamingCorrection):
    revision: int = Field(ge=0, strict=True)


class NamingPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    link: str = Field(max_length=8192)
    password: str = Field(default="", max_length=32)
    selected_ids: list[str] = Field(default_factory=list, max_length=1000)
    magnet_selection: MagnetSelection | None = None
    media: MediaSelection | None = None
    resolution: str | None = Field(default=None, max_length=16)


class SubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bangumi_id: int = Field(gt=0)
    subgroup_id: int | None = Field(default=None, gt=0)
    media: MediaSelection
    target: str = Field(max_length=2000)
    rules: dict = Field(default_factory=dict)
    backfill: bool = True


class SubscriptionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    target: str | None = Field(default=None, max_length=2000)
    rules: dict | None = None


class TaskRequest(BaseModel):
    link: str = Field(max_length=8192)
    title: str = Field(default="未命名资源", max_length=220)
    target: str = Field(max_length=2000)
    password: str = Field(default="", max_length=32)
    selected_ids: list[str] = Field(default_factory=list, max_length=1000)
    sources: list[dict] = Field(default_factory=list, max_length=20)
    auto_naming: bool = False
    magnet_selection: MagnetSelection | None = None
    media: MediaSelection | None = None
    resolution: str | None = Field(default=None, max_length=16)
    naming_preview: NamingPreviewReference | None = None


def public_task(task, naming_files=True):
    payload = task["payload"]
    return {k: v for k, v in task.items() if k != "payload"} | {
        "title": payload.get("title"), "kind": payload["kind"], "sources": payload.get("sources", []),
        "staging": payload.get("staging"), "retry_at": payload.get("retry_at"),
        "can_change_target": payload.get("resume_state") == "organizing" or (
            payload.get("resume_state") == "placing"
            and all(item["status"] == "pending" for item in payload.get("placement", []))),
        "notified_at": payload.get("notified_at"), "subscription": payload.get("subscription"),
        "naming": public_naming(task, include_files=naming_files),
        "selection": {"selected_count": len(payload["magnet_selection"]["files"]),
                      "retained_count": payload.get("retained_count", payload["magnet_selection"]["total_count"] - len(payload["magnet_selection"]["files"])),
                      "download_staging": payload.get("download_staging") or payload.get("staging")}
        if payload.get("magnet_selection") else None}


def create_app(store=None, vault=None, start_workers=True, frontend_dist=None):
    store, vault = store or Store(), vault or Vault()
    migrate_public_access(store, os.environ.get("MEDIALINK_ALLOWED_HOSTS", ""))
    openlist = OpenList(store.settings, vault)
    p115 = P115(vault, store.settings, openlist)
    p115.settings_store = store
    telegram = Telegram(store, vault)
    nyaa = Nyaa(store.settings)
    knaben, limetorrents = Knaben(store.settings), LimeTorrents(store.settings)
    telegram_web = TelegramWeb(store)
    sources = {"knaben": knaben, "limetorrents": limetorrents, "nyaa": nyaa, "telegram": telegram}
    source_names = {"knaben": "Knaben", "limetorrents": "LimeTorrents", "nyaa": "Nyaa",
                    "telegram": "Telegram 频道"}
    tmdb = TMDB(store, vault)
    mikan = Mikan(store.settings, store.path.parent)
    anime_schedule = AnimeSchedule(store, tmdb, mikan, BgmTv(store.settings, store))
    home = HomeFeed(store, LocalHome(store, tmdb, Yts(store.settings), Eztv(store.settings), anime_schedule))
    # Subscriptions submit through the same path as the ingest dialog (defined below).
    subscriptions = Subscriptions(store, mikan, tmdb, lambda payload, extra: submit_task(TaskRequest(**payload), extra))
    search_engine = SearchEngine(sources, tmdb)
    worker = Worker(store, vault, openlist, p115)
    magnets = MagnetPreviews(store)
    naming_previews = NamingPreviews(store, vault, magnets, p115, tmdb, worker.llm)
    searches, attempts, search_tasks = {}, {}, set()
    auth_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        jobs = [asyncio.create_task(worker.run()), asyncio.create_task(telegram.run()),
                asyncio.create_task(telegram_web.run())] if start_workers else []
        if start_workers:
            jobs.append(asyncio.create_task(magnets.warmup()))
            jobs.append(asyncio.create_task(home.run()))
            jobs.append(asyncio.create_task(subscriptions.run()))
        yield
        worker.stopped, telegram.stopped, telegram_web.stopped, home.stopped = True, True, True, True
        subscriptions.stopped = True
        jobs.extend(list(search_tasks))
        for job in jobs:
            job.cancel()
        for job in jobs:
            try:
                await job
            except asyncio.CancelledError:
                pass
        await worker.close()
        await anime_schedule.close()
        await naming_previews.close()
        await telegram.reset()
        await magnets.close()

    app = FastAPI(title="MediaLink", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.worker = store, worker
    app.state.openlist, app.state.p115, app.state.telegram, app.state.nyaa = openlist, p115, telegram, nyaa
    app.state.knaben, app.state.limetorrents, app.state.telegram_web = knaben, limetorrents, telegram_web
    app.state.tmdb, app.state.search_engine = tmdb, search_engine
    app.state.magnets = magnets
    app.state.home = home
    app.state.mikan, app.state.anime_schedule, app.state.subscriptions = mikan, anime_schedule, subscriptions
    app.state.llm = worker.llm
    app.state.naming_previews = naming_previews

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        # Do not reflect password/cookie/API hash inputs in validation responses.
        return JSONResponse({"detail": "输入格式不正确，请检查填写内容"}, status_code=422)

    @app.middleware("http")
    async def authenticate(request, call_next):
        host = request.url.hostname or ""
        if not host_allowed(host, store.settings()):
            return JSONResponse({"detail": "访问主机不在允许列表"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "请求来源不匹配"}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path not in {
            "/api/v1/auth/state", "/api/v1/auth/setup", "/api/v1/auth/login", "/api/v1/health"}:
            if not store.session_valid(request.cookies.get("medialink_session", "")):
                return JSONResponse({"detail": "请先登录"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/v1/auth/state")
    async def auth_state(request: Request):
        return {"configured": bool(await blocking(vault.get, "password")),
                "authenticated": store.session_valid(request.cookies.get("medialink_session", "")),
                "setup_mode": os.environ.get("MEDIALINK_SETUP_MODE", "browser")}

    def set_session(response):
        response.set_cookie("medialink_session", store.new_session(), httponly=True, samesite="strict", max_age=86400*7)

    def limit_attempts(ip):
        now = time.time()
        attempts[ip] = [t for t in attempts.get(ip, []) if t > now-60]
        if len(attempts[ip]) >= 5:
            raise Problem("尝试过于频繁，请一分钟后再试", 429)
        attempts[ip].append(now)

    async def verify_password(password, code):
        saved = await blocking(vault.get, "password")
        if not saved:
            raise Problem("请先在 Mac 本机设置密码", 409)
        record = json.loads(saved)
        hashed = await blocking(password_hash, password, record["salt"])
        if not secrets.compare_digest(hashed, record["hash"]):
            raise Problem("当前密码不正确" if code == 403 else "密码不正确", code)

    async def save_password(password):
        salt = secrets.token_hex(16)
        hashed = await blocking(password_hash, password, salt)
        await blocking(vault.set, "password", json.dumps({"salt": salt, "hash": hashed}))

    @app.post("/api/v1/auth/setup")
    async def setup(data: Credentials, request: Request, response: Response):
        if os.environ.get("MEDIALINK_SETUP_MODE") == "cli":
            raise Problem("当前部署请从服务终端初始化访问密码", 403)
        if request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise Problem("首次设置请从 Mac 本机打开页面", 403)
        async with auth_lock:
            if await blocking(vault.get, "password"):
                raise Problem("密码已设置，请登录", 409)
            await save_password(data.password)
            set_session(response)
        return {"ok": True}

    @app.post("/api/v1/auth/login")
    async def login(data: Credentials, request: Request, response: Response):
        ip = request.client.host
        async with auth_lock:
            limit_attempts(ip)
            await verify_password(data.password, 401)
            attempts.pop(ip, None)
            set_session(response)
        return {"ok": True}

    @app.post("/api/v1/auth/password")
    async def change_password(data: PasswordChange, request: Request, response: Response):
        async with auth_lock:
            # A request may have passed middleware while waiting for another password change.
            if not store.session_valid(request.cookies.get("medialink_session", "")):
                raise Problem("请先登录", 401)
            limit_attempts(request.client.host)
            await verify_password(data.current_password, 403)
            if data.current_password == data.new_password:
                raise Problem("新密码不能与当前密码相同", 422)
            await save_password(data.new_password)
            store.revoke_sessions()
            attempts.pop(request.client.host, None)
            response.delete_cookie("medialink_session")
        return {"ok": True}

    @app.post("/api/v1/auth/logout")
    async def logout(request: Request, response: Response):
        token = request.cookies.get("medialink_session", "")
        with store.connect() as db:
            db.execute("DELETE FROM sessions WHERE digest=?", (hashlib.sha256(token.encode()).hexdigest(),))
        response.delete_cookie("medialink_session")
        return {"ok": True}

    @app.get("/api/v1/settings")
    async def get_settings():
        settings = store.settings()
        return {**settings, "telegram_hash_saved": bool(await blocking(vault.get, "telegram-api-hash")),
                "telegram_connected": bool(await blocking(vault.get, "telegram-session")),
                "p115_connected": bool(await blocking(vault.get, "115-cookies")),
                **await tmdb.state(), **await worker.llm.state()}

    @app.patch("/api/v1/settings")
    async def configure(data: dict):
        allowed = set(DEFAULTS) | {"telegram_api_hash", "openlist_token", "emby_token", "tmdb_credential", "llm_api_key"}
        if not set(data) <= allowed:
            raise Problem("包含不支持的设置", 422)
        previous = store.settings()
        settings = {**previous, **{k: v for k, v in data.items() if k in DEFAULTS}}
        if any(not isinstance(settings[k], str) for k in DEFAULTS
               if k not in {"poll_seconds", "public_access_enabled", "enabled_search_sources", "default_use_tmdb",
                            "llm_enabled", "llm_use_proxy", "magnet_use_proxy"}):
            raise Problem("设置字段必须是文本", 422)
        llm_key = await blocking(vault.get, "llm-api-key")
        if "llm_api_key" in data:
            supplied_key = data["llm_api_key"]
            if supplied_key is None:
                llm_key = None
                if "llm_enabled" not in data:
                    settings["llm_enabled"] = False
            elif not isinstance(supplied_key, str):
                raise Problem("大模型 API Key 格式无效", 422)
            elif supplied_key.strip():
                llm_key = supplied_key.strip()
        validate_llm_settings(settings, llm_key)
        enabled = settings["enabled_search_sources"]
        if not isinstance(enabled, list) or any(not isinstance(key, str) or key not in sources for key in enabled):
            raise Problem("请选择有效的搜索来源", 422)
        if not isinstance(settings["default_use_tmdb"], bool):
            raise Problem("TMDB 辅助开关必须为布尔值", 422)
        if not isinstance(settings["magnet_use_proxy"], bool):
            raise Problem("磁力代理开关必须为布尔值", 422)
        if not isinstance(settings['public_access_enabled'], bool):
            raise Problem('公网访问开关必须为布尔值', 422)
        settings['public_access_hosts'] = normalize_public_hosts(settings['public_access_hosts'])
        if settings['public_access_enabled'] and not settings['public_access_hosts']:
            raise Problem('开启公网访问前请填写允许的域名或 IP', 422)
        for key in ("openlist_url", "emby_url", "nyaa_url", "knaben_url", "limetorrents_url", "mikan_url",
                    "yts_url", "eztv_url"):
            parsed = urlparse(str(settings[key]))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise Problem("服务地址必须是有效的 HTTP／HTTPS 地址", 422)
        if settings["proxy"]:
            proxy = urlparse(str(settings["proxy"]))
            if proxy.scheme not in {"http", "https", "socks5", "socks5h"} or not proxy.hostname or not proxy.port:
                raise Problem("代理地址格式无效", 422)
        for key in ("mount_root", "media_root", "strm_root", "staging_root"):
            target_path(settings[key], "/")
        target_path(settings["media_root"], settings["mount_root"])
        target_path(settings["staging_root"], settings["mount_root"])
        if PurePosixPath(settings["staging_root"]).is_relative_to(settings["media_root"]):
            raise Problem("暂存目录应位于媒体目录外", 422)
        target_path(settings["last_target"], settings["media_root"])
        if settings["anime_target"]:
            settings["anime_target"] = target_path(settings["anime_target"].strip(), settings["media_root"])
        if not isinstance(settings["poll_seconds"], int) or not 5 <= settings["poll_seconds"] <= 300:
            raise Problem("轮询间隔必须在 5–300 秒", 422)
        if settings["telegram_api_id"] and not str(settings["telegram_api_id"]).isdigit():
            raise Problem("Telegram API ID 必须为数字", 422)
        values = {k: v for k, v in data.items() if k in DEFAULTS}
        for key in ("llm_base_url", "llm_model"):
            if key in data:
                values[key] = settings[key]
        if data.get("llm_api_key", "") is None:
            values["llm_enabled"] = settings["llm_enabled"]
        if "enabled_search_sources" in data:
            values["enabled_search_sources"] = list(dict.fromkeys(enabled))
        if "anime_target" in data:
            values["anime_target"] = settings["anime_target"]
        if 'public_access_hosts' in data:
            values['public_access_hosts'] = settings['public_access_hosts']
        # Proxy credentials are stored separately; the settings API only accepts a host/port proxy.
        if settings["proxy"] and (urlparse(settings["proxy"]).username or urlparse(settings["proxy"]).password):
            raise Problem("第一版代理请使用不含账号密码的本机代理地址", 422)
        unfinished = [t for t in store.tasks() if t["state"] not in {"completed", "canceled"}]
        if unfinished and any(k in data and data[k] != store.settings()[k] for k in ("media_root", "strm_root", "mount_root", "staging_root")):
            raise Problem("存在活动任务，暂不能修改目录映射", 409)
        for field, key in (("telegram_api_hash", "telegram-api-hash"), ("openlist_token", "openlist-token"), ("emby_token", "emby-token")):
            if data.get(field):
                await blocking(vault.set, key, str(data[field]))
        if "tmdb_credential" in data:
            await tmdb.configure(data["tmdb_credential"])
        if "llm_api_key" in data:
            if data["llm_api_key"] is None:
                await blocking(vault.delete, "llm-api-key")
            elif data["llm_api_key"].strip():
                await blocking(vault.set, "llm-api-key", llm_key)
        store.configure(values)
        if any(k in data and data[k] != previous[k] for k in ("yts_url", "eztv_url", "mikan_url", "proxy")):
            # Connection changes invalidate the previous failure; refresh on the next pass.
            home.error, home.retry_at = None, 0
        if any(k in data for k in ("telegram_api_id", "telegram_api_hash", "proxy")):
            await telegram.reset()
        if any(k in data and data[k] != previous[k] for k in ("mikan_url", "proxy")):
            mikan.cache.clear()
        return await get_settings()

    @app.post("/api/v1/llm/check")
    async def check_llm():
        return await worker.llm.check()

    @app.get("/api/v1/settings/check")
    async def check_connections():
        async def openlist_check():
            await openlist.get(store.settings()["media_root"])
            path = await openlist.resolve_strm_path(store.settings()["strm_root"])
            return {"ok": True, "message": "媒体目录可访问，STRM：" + path}
        async def emby_check():
            import httpx
            async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
                response = await client.get(store.settings()["emby_url"].rstrip("/")+"/System/Info/Public")
                response.raise_for_status()
                return {"ok": True, "message": "Emby " + response.json().get("Version", "")}
        async def p115_check():
            if not await blocking(vault.get, "115-cookies"):
                return {"ok": False, "message": "尚未登录"}
            await p115.folder_id(store.settings()["media_root"])
            return {"ok": True, "message": "账号与 OpenList 一致"}
        async def tg_check():
            if not store.settings()["telegram_api_id"]:
                return {"ok": True, "message": "个人账号 API 模式未启用（可选）"}
            ok = await telegram.authorized()
            return {"ok": ok, "message": "已登录" if ok else "尚未登录或不可连接"}
        async def tg_web_check():
            await telegram_web.fetch_page("telegram")
            return {"ok": True, "message": "公开网页可访问，无需个人 API"}
        checks = await asyncio.gather(openlist_check(), emby_check(), p115_check(), tg_check(), tg_web_check(), tmdb.check(), return_exceptions=True)
        return {name: result if isinstance(result, dict) else {"ok": False, "message": result.message if isinstance(result, Problem) else "连接失败"}
                for name, result in zip(("openlist", "emby", "p115", "telegram", "telegram_web", "tmdb"), checks)}

    @app.get("/api/v1/tmdb/check")
    async def tmdb_check():
        return await tmdb.check()

    @app.get("/api/v1/tmdb/trending")
    async def tmdb_trending(media_type: str = "all", window: str = "week", page: int = Query(1, ge=1, le=500)):
        return await tmdb.trending(media_type, window, page)

    @app.get("/api/v1/tmdb/filters")
    async def tmdb_filters(media_type: str = "movie"):
        return await tmdb.filters(media_type)

    @app.get("/api/v1/tmdb/discover")
    async def tmdb_discover(
        media_type: str = "movie", include_genres: str = Query("", max_length=200),
        exclude_genres: str = Query("", max_length=200),
        year_from: int | None = Query(None, ge=1000, le=9999),
        year_to: int | None = Query(None, ge=1000, le=9999),
        country: str = Query("", max_length=2), original_language: str = Query("", max_length=2),
        page: int = Query(1, ge=1, le=500),
    ):
        return await tmdb.discover(media_type, include_genres, exclude_genres, year_from, year_to,
                                   country, original_language, page)

    @app.get("/api/v1/tmdb/search")
    async def tmdb_search(query: str = Query(min_length=1, max_length=200), page: int = Query(1, ge=1, le=500)):
        return await tmdb.search(query, page)

    @app.get("/api/v1/tmdb/images/{size}/{path:path}")
    async def tmdb_image(size: str, path: str):
        data, mime = await tmdb.image(size, path)
        return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/v1/tmdb/{media_type}/{media_id}")
    async def tmdb_detail(media_type: str, media_id: int):
        return await tmdb.detail(media_type, media_id)

    @app.get("/api/v1/sources")
    async def available_sources():
        return [{"id": key, "name": source_names[key], "kind": "index" if key == "telegram" else "website"}
                for key in sources]

    @app.get("/api/v1/home")
    async def home_feed():
        return await home.feed()

    @app.post("/api/v1/home/refresh")
    async def home_refresh():
        await home.refresh(force=True)
        return await home.feed()

    def subscribed_ids():
        return {s["bangumi_id"] for s in store.subscriptions()}

    @app.get("/api/v1/anime/schedule")
    async def anime_schedule_feed(year: int | None = Query(None), season: str | None = Query(None, max_length=2)):
        data = await anime_schedule.schedule(year, season)
        subscribed = await blocking(subscribed_ids)
        for day in data["days"]:
            for item in day["items"]:
                item["subscribed"] = item["id"] in subscribed
        return data

    @app.post("/api/v1/anime/schedule/refresh")
    async def anime_schedule_refresh(year: int | None = Query(None), season: str | None = Query(None, max_length=2)):
        await anime_schedule.schedule(year, season, force=True)
        return await anime_schedule_feed(year, season)

    @app.get("/api/v1/anime/bangumi/{bangumi_id}")
    async def anime_bangumi(bangumi_id: int):
        if bangumi_id <= 0:
            raise Problem("番剧标识无效", 422)
        detail = await mikan.bangumi(bangumi_id)
        settings = store.settings()
        return {**detail, "media": await anime_schedule.suggestion(detail),
                "target": settings["anime_target"] or settings["last_target"]}

    @app.get("/api/v1/anime/images/{path:path}")
    async def anime_image(path: str):
        data, mime = await mikan.image(path)
        return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})

    def public_subscription(subscription, episodes=None):
        return {**subscription, "episodes": episodes if episodes is not None
                else store.subscription_episodes(subscription["id"])}

    @app.get("/api/v1/subscriptions")
    async def list_subscriptions():
        return [public_subscription(s) for s in store.subscriptions()]

    @app.post("/api/v1/subscriptions")
    async def create_subscription(data: SubscriptionRequest):
        rules = normalize_rules(data.rules)
        target = target_path(data.target.strip(), store.settings()["media_root"])
        if data.media.type == "tv" and data.media.season_number is None:
            raise Problem("请填写订阅对应的 TMDB 季数", 422)
        detail = await mikan.bangumi(data.bangumi_id)
        group = next((g for g in detail["subgroups"] if g["id"] == data.subgroup_id), None)
        if data.subgroup_id is not None and not group:
            raise Problem("该番剧没有所选字幕组，请刷新后重试", 422)
        await tmdb.detail(data.media.type, data.media.id)
        await openlist.get(target)
        subscription = await blocking(store.create_subscription, {
            "bangumi_id": data.bangumi_id, "subgroup_id": data.subgroup_id,
            "subgroup_name": group["name"] if group else None, "title": detail["title"], "poster": detail["poster"],
            "media_type": data.media.type, "media_id": data.media.id,
            "season_number": data.media.season_number if data.media.type == "tv" else None,
            "target": target, "rules": rules, "backfill": data.backfill})
        return public_subscription(await subscriptions.check(subscription["id"]))

    @app.get("/api/v1/subscriptions/{subscription_id}")
    async def get_subscription(subscription_id: str):
        return public_subscription(store.subscription(subscription_id))

    @app.patch("/api/v1/subscriptions/{subscription_id}")
    async def update_subscription(subscription_id: str, data: SubscriptionUpdate):
        store.subscription(subscription_id)
        updates = {}
        if data.enabled is not None:
            updates["enabled"] = int(data.enabled)
            if data.enabled:
                updates["next_check"] = 0
        if data.rules is not None:
            updates["rules"] = normalize_rules(data.rules)
        if data.target is not None:
            updates["target"] = target_path(data.target.strip(), store.settings()["media_root"])
            await openlist.get(updates["target"])
        subscription = store.subscription(subscription_id, **updates)
        subscriptions.wake.set()
        return public_subscription(subscription)

    @app.delete("/api/v1/subscriptions/{subscription_id}", status_code=204)
    async def delete_subscription(subscription_id: str):
        # Waits for a running check so no task is submitted for a removed subscription.
        async with subscriptions.lock:
            store.delete_subscription(subscription_id)
        return Response(status_code=204)

    @app.post("/api/v1/subscriptions/{subscription_id}/check")
    async def check_subscription(subscription_id: str):
        store.subscription(subscription_id)
        return public_subscription(await subscriptions.check(subscription_id))

    async def execute_search(search_id, data):
        await search_engine.execute(data, searches[search_id])

    @app.post("/api/v1/search")
    async def search(data: SearchRequest):
        settings = store.settings()
        data = data.model_copy(update={
            "sources": settings["enabled_search_sources"]
            if "sources" not in data.model_fields_set or data.sources is None else list(dict.fromkeys(data.sources)),
            "use_tmdb": settings["default_use_tmdb"]
            if "use_tmdb" not in data.model_fields_set or data.use_tmdb is None else data.use_tmdb,
        })
        if not data.query.strip() or not data.sources or not set(data.sources) <= set(sources):
            raise Problem("请输入关键词并选择有效来源", 422)
        if not set(data.sources) <= set(settings["enabled_search_sources"]):
            raise Problem("所选搜索来源已停用，请刷新页面或在设置中启用", 422)
        if data.media and data.media.type == "movie" and data.media.season_number is not None:
            raise Problem("电影不能选择季数", 422)
        for key in list(searches):
            if searches[key]["created"] < time.time()-600:
                searches.pop(key)
        if len(searches) >= 100:
            raise Problem("搜索过于频繁，请稍后重试", 429)
        search_id = str(uuid.uuid4())
        searches[search_id] = {"created": time.time(), "events": [], "results": [], "done": False}
        task = asyncio.create_task(execute_search(search_id, data))
        search_tasks.add(task)
        task.add_done_callback(search_tasks.discard)
        return {"id": search_id, "coverage": store.channels()}

    @app.get("/api/v1/search/{search_id}/events")
    async def search_events(search_id: str, request: Request):
        if search_id not in searches:
            raise Problem("搜索已过期，请重新搜索", 404)
        job = searches[search_id]
        async def stream():
            index = 0
            deadline = time.time()+120
            while time.time() < deadline:
                if await request.is_disconnected():
                    return
                if not store.session_valid(request.cookies.get("medialink_session", "")):
                    yield 'event: auth_required\ndata: {"error":"登录已失效，请重新登录"}\n\n'
                    return
                while index < len(job["events"]):
                    event = dict(job["events"][index])
                    kind = event.pop("_event", "result")
                    yield "event: " + kind + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                    index += 1
                if job["done"]:
                    yield "event: done\ndata: " + json.dumps({"results": job["results"]}, ensure_ascii=False) + "\n\n"
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)
            yield 'event: expired\ndata: {"error":"搜索等待超时，请重试"}\n\n'
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get("/api/v1/directories")
    async def directories(path: str = "", refresh: bool = False):
        root = store.settings()["media_root"]
        path = target_path(path or root, root)
        entries = await openlist.entries(path, refresh=refresh)
        return {"path": path, "root": root, "directories": [{"name": e["name"], "path": str(PurePosixPath(path)/e["name"])}
                                               for e in entries if e.get("is_dir")]}

    def directory_name(value):
        if not isinstance(value, str):
            raise Problem("目录名称无效", 422)
        name = value.strip()
        if (not name or name in {".", ".."} or len(name.encode("utf-8")) > 255
                or any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in name)):
            raise Problem("目录名称无效，不能包含斜线或控制字符", 422)
        return name

    def name_conflicts(name, entries, excluding=None):
        import unicodedata
        key = unicodedata.normalize("NFC", name).casefold()
        return any(e["name"] != excluding and unicodedata.normalize("NFC", e["name"]).casefold() == key
                   for e in entries)

    @app.post("/api/v1/directories")
    async def create_directory(data: dict):
        root = target_path(data.get("parent"), store.settings()["media_root"])
        name = directory_name(data.get("name"))
        path = target_path(str(PurePosixPath(root)/name), store.settings()["media_root"])
        async with worker.lock:
            if name_conflicts(name, await openlist.entries(root)):
                raise Problem("此位置已有同名文件或文件夹，请换一个名称", 409)
            await openlist.mkdir(path)
        return {"path": path}

    @app.patch("/api/v1/directories")
    async def rename_directory(data: dict):
        media_root = store.settings()["media_root"]
        path = target_path(data.get("path"), media_root)
        name = directory_name(data.get("name"))
        if path == media_root:
            raise Problem("媒体根目录不能重命名", 422)
        parent = str(PurePosixPath(path).parent)
        destination = str(PurePosixPath(parent) / name)
        async with worker.lock:
            entries = await openlist.entries(parent)
            source = next((e for e in entries if e["name"] == PurePosixPath(path).name), None)
            if not source or not source.get("is_dir"):
                raise Problem("此文件夹不存在，请刷新目录后重试", 404)
            if destination != path:
                if name_conflicts(name, entries, source["name"]):
                    raise Problem("此位置已有同名文件或文件夹，请换一个名称", 409)
                with store.connect() as db:
                    active = db.execute("SELECT target FROM tasks WHERE state NOT IN ('completed','canceled')").fetchall()
                for task in active:
                    target = PurePosixPath(task["target"])
                    if target.is_relative_to(path) or PurePosixPath(path).is_relative_to(target):
                        raise Problem("相关目录正在用于入库，请完成或取消任务后再重命名", 409)
                try:
                    await openlist.rename(path, name)
                except RemoteFailure as e:
                    if e.uncertain:
                        raise Problem("重命名响应超时，结果待确认；请刷新目录核对，勿重复提交", 502) from None
                    raise
            last = store.settings()["last_target"]
            if PurePosixPath(last).is_relative_to(path):
                last = str(PurePosixPath(destination) / PurePosixPath(last).relative_to(path))
                store.configure({"last_target": last})
        return {"path": destination, "previous_path": path, "last_target": last}

    @app.get("/api/v1/tasks")
    async def tasks():
        return [public_task(t, naming_files=False) for t in store.tasks()]

    @app.get("/api/v1/naming/layout")
    async def naming_layout(media_type: str, media_id: int = Query(gt=0)):
        return {"folder": library_folder(media_snapshot(await tmdb.detail(media_type, media_id)))}

    @app.post("/api/v1/naming/previews")
    async def create_naming_preview(data: NamingPreviewRequest):
        checked_resolution(data.resolution)
        return naming_previews.public(await naming_previews.create(data.model_dump()))

    @app.get("/api/v1/naming/previews/{identifier}")
    async def get_naming_preview(identifier: str):
        draft = naming_previews.get(identifier)
        # Polling is activity, but does not change the revision being confirmed.
        store.cache_metadata(naming_previews.key(identifier), draft, 1800)
        return naming_previews.public(draft)

    @app.patch("/api/v1/naming/previews/{identifier}")
    async def correct_naming_preview(identifier: str, data: NamingPreviewCorrection):
        return naming_previews.public(naming_previews.correct(identifier, data))

    @app.delete("/api/v1/naming/previews/{identifier}", status_code=204)
    async def delete_naming_preview(identifier: str):
        naming_previews.delete(identifier)
        return Response(status_code=204)

    @app.post("/api/v1/naming/previews/{identifier}/analyze")
    async def analyze_naming_preview(identifier: str):
        return naming_previews.public(naming_previews.reanalyze(identifier))

    @app.post("/api/v1/tasks")
    async def submit(data: TaskRequest):
        return await submit_task(data)

    async def submit_task(data: TaskRequest, extra=None):
        key = resource_key(data.link)
        target = target_path(data.target, store.settings()["media_root"])
        naming, selection = None, None
        reference = data.naming_preview.model_dump() if data.naming_preview else None
        if reference:
            naming, selection = naming_previews.confirm(reference["preview_id"], reference["revision"], data.model_dump())

        def check_existing(task):
            if selection_signature(task["payload"].get("magnet_selection")) != selection_signature(data.magnet_selection.model_dump() if data.magnet_selection else None):
                raise Problem("同一磁力和目标目录已有不同文件选择的任务，请查看已有任务或更换目录", 409)
            approved = (task["payload"].get("naming") or {}).get("preview", {})
            if reference and (not approved.get("confirmed") or any(approved.get(k) != v for k, v in reference.items())):
                raise Problem("已有任务与当前命名预览不一致，请查看已有任务或更换目录", 409)

        with store.connect() as db:
            existing = db.execute("SELECT id FROM tasks WHERE resource_key=? AND target=?", (key, target)).fetchone()
        if existing:
            task = store.task(existing["id"])
            check_existing(task)
            return public_task(task)
        if data.magnet_selection and not reference:
            if not key.startswith("magnet:"):
                raise Problem("文件预览选择仅用于磁力链接", 422)
            selection = magnets.select(key, data.magnet_selection)
        if key.startswith("share:"):
            entries = await p115.share_entries(data.link, data.password)
            if not entries:
                raise Problem("分享内容为空", 422)
            if data.selected_ids and not set(data.selected_ids) <= {e["id"] for e in entries}:
                raise Problem("选中的分享内容已失效，请重新预览", 422)
        if data.auto_naming and not reference:
            resolution = checked_resolution(data.resolution)
            media = media_snapshot(await tmdb.detail(data.media.type, data.media.id)) if data.media else None
            naming = {"version": 2, "media": media, "season_number": data.media.season_number if data.media else None,
                      "resolution": resolution, "files": [], "overrides": {}, "plan": [], "locked": False}
        secret = await blocking(vault.seal, json.dumps({"link": data.link, "password": data.password}))
        async with worker.lock:
            await openlist.get(target)
            if reference:
                # Network validation above may outlive a correction, cancellation or expiry.
                naming, selection = naming_previews.confirm(reference["preview_id"], reference["revision"], data.model_dump())
            task = store.create_task(key, target, {"title": data.title, "kind": "magnet" if key.startswith("magnet:") else "share",
                                                 "link_enc": secret, "selected_ids": data.selected_ids, "sources": data.sources,
                                                 "naming": naming, "magnet_selection": selection,
                                                 # Version 2 naming curates files and builds an Emby title folder.
                                                 "pipeline": 2 if naming and naming.get("version") == 2 else 1,
                                                 **(extra or {})})
            check_existing(task)
            store.configure({"last_target": target})
        return public_task(task)

    @app.get("/api/v1/tasks/{task_id}")
    async def get_task(task_id: str):
        task = public_task(store.task(task_id))
        with store.connect() as db:
            task["events"] = [dict(r) for r in db.execute("SELECT state,message,created FROM events WHERE task_id=? ORDER BY id", (task_id,))]
        return task

    @app.get("/api/v1/tasks/{task_id}/naming")
    async def get_naming(task_id: str):
        result = public_naming(store.task(task_id))
        if result is None:
            raise Problem("此任务未启用自动规范命名，保留原文件名", 409)
        return result

    @app.patch("/api/v1/tasks/{task_id}/naming")
    async def correct_naming(task_id: str, data: NamingCorrection):
        async with worker.lock:
            task = store.task(task_id)
            view = public_naming(task)
            if not view or not view["editable"]:
                raise Problem("此任务命名计划已锁定或不支持修改", 409)
            naming = task["payload"]["naming"]
            if "media" in data.model_fields_set:
                if data.media is None:
                    naming["media"], naming["season_number"] = None, None
                else:
                    naming["media"] = media_snapshot(await tmdb.detail(data.media.type, data.media.id))
                    naming["season_number"] = data.media.season_number
                naming["overrides"] = {}
                naming["suggestions"] = {}
            if "resolution" in data.model_fields_set:
                naming["resolution"] = checked_resolution(data.resolution)
            apply_file_corrections(naming, data.files)
            if naming.get("preview"):
                naming["preview"]["confirmed"] = False
            worker.cancel_naming_job(task_id)
            naming["revision"] = naming.get("revision", 0) + 1
            if naming.get("llm"):
                naming["review_required"] = True
                if naming["llm"].get("status") == "running" or "media" in data.model_fields_set:
                    naming["llm"] = {"status": "superseded", "message": "命名信息已修改，可人工补全或重新识别"}
            if naming["files"]:
                naming = build_plan(naming)
            resume = task["payload"].get("resume_state")
            if task["state"] in {"naming_planning", "renaming", "naming_analyzing", "naming_review"} or (task["state"] == "needs_attention" and resume in {"naming_planning", "renaming"}):
                state = "needs_attention" if naming.get("errors") else "naming_review" if naming.get("review_required") else "naming_planning"
                task = store.update_task(task_id, state=state, naming=naming, resume_state="naming_planning",
                                         error="；".join(naming.get("errors", [])) or None, failures=0, retry_at=0, next_poll=0)
            else:
                task = store.update_task(task_id, naming=naming, error=task.get("error"))
            return public_naming(task)

    @app.post("/api/v1/tasks/{task_id}/naming/analyze")
    async def analyze_naming(task_id: str):
        async with worker.lock:
            task = store.task(task_id)
            view = public_naming(task)
            if not view or not view["editable"] or task["state"] not in {"naming_planning", "naming_review", "needs_attention", "naming_analyzing"}:
                raise Problem("此阶段不支持大模型识别", 409)
            if task["state"] == "needs_attention" and task["payload"].get("resume_state") not in {"naming_planning", "renaming"}:
                raise Problem("请先处理当前入库阶段", 409)
            if task["state"] == "naming_analyzing":
                return view
            await worker.verify_naming_manifest(task)
            return public_naming(worker.start_naming_analysis(task))

    @app.post("/api/v1/tasks/{task_id}/naming/confirm")
    async def confirm_naming(task_id: str, data: NamingConfirmation):
        async with worker.lock:
            task = store.task(task_id)
            naming = task["payload"].get("naming")
            if not naming or data.revision != naming.get("revision", 0):
                raise Problem("命名预览已更新，请刷新后重新确认", 409)
            if naming.get("confirmed_revision") == data.revision and not naming.get("review_required"):
                return public_naming(task)
            if task["state"] != "naming_review" or not naming.get("review_required") or naming.get("locked"):
                raise Problem("当前命名计划不支持确认，请先保存有效预览", 409)
            rebuilt = build_plan(naming)
            if rebuilt["errors"]:
                raise Problem("请先补全或修正命名信息", 409)
            await worker.verify_naming_manifest(task)
            rebuilt.update(review_required=False, confirmed_revision=data.revision)
            task = store.update_task(task_id, state="renaming", naming=rebuilt, resume_state="renaming",
                                     failures=0, retry_at=0, next_poll=0)
            return public_naming(task)

    @app.post("/api/v1/tasks/{task_id}/retry")
    async def retry(task_id: str, data: dict):
        return public_task(await worker.retry(task_id, data.get("target")))

    @app.post("/api/v1/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        return public_task(await worker.cancel(task_id))

    @app.post("/api/v1/shares/preview")
    async def preview_share(data: dict):
        return await p115.share_entries(str(data.get("link", "")), str(data.get("password", "")))

    @app.post("/api/v1/magnets/preview")
    async def preview_magnet(data: MagnetRequest, request: Request):
        preview = asyncio.create_task(magnets.preview(data.link))

        async def disconnected():
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    return

        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait({preview, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if preview in done:
                return await preview
            raise Problem("磁力预览已取消", 499)
        finally:
            for task in (preview, watcher):
                if not task.done():
                    task.cancel()
            await asyncio.gather(preview, watcher, return_exceptions=True)

    @app.get("/api/v1/channels")
    async def channels():
        return store.channels()

    @app.get("/api/v1/telegram/dialogs")
    async def dialogs():
        return await telegram.dialogs()

    @app.post("/api/v1/channels")
    async def add_channel(data: dict):
        mode = data.get("mode", "public_web")
        if not isinstance(mode, str) or mode not in {"public_web", "api"} or not isinstance(data.get("peer"), str):
            raise Problem("请填写频道和有效的同步方式", 422)
        return await (telegram_web if mode == "public_web" else telegram).add_channel(data["peer"])

    @app.patch("/api/v1/channels/{channel_id}")
    async def update_channel(channel_id: str, data: dict):
        if set(data) != {"enabled"} or not isinstance(data["enabled"], bool):
            raise Problem("请指定是否启用频道", 422)
        return store.channel(channel_id, enabled=int(data["enabled"]), status="等待同步" if data["enabled"] else "已暂停",
                             wait_until=0, refresh_at=0, failures=0)

    @app.delete("/api/v1/channels/{channel_id}")
    async def delete_channel(channel_id: str):
        store.channel(channel_id)
        with store.connect() as db:
            db.execute("DELETE FROM message_fts WHERE channel_id=?", (channel_id,))
            db.execute("DELETE FROM messages WHERE channel_id=?", (channel_id,))
            db.execute("DELETE FROM deleted_messages WHERE channel_id=?", (channel_id,))
            db.execute("DELETE FROM channels WHERE id=?", (channel_id,))
        return {"ok": True}

    @app.post("/api/v1/telegram/code")
    async def telegram_code(data: dict):
        return await telegram.send_code(str(data.get("phone", "")))

    @app.post("/api/v1/telegram/login")
    async def telegram_login(data: dict):
        return await telegram.sign_in(str(data.get("code", "")), str(data.get("password", "")))

    @app.post("/api/v1/telegram/logout")
    async def telegram_logout():
        await telegram.logout()
        return {"ok": True}

    @app.post("/api/v1/p115/qr")
    async def p115_qr():
        return await p115.qr_start()

    @app.post("/api/v1/p115/qr/poll")
    async def p115_poll():
        return await p115.qr_poll()

    @app.get("/api/v1/p115/qr/image")
    async def p115_image():
        import qrcode
        from qrcode.image.svg import SvgPathImage
        if not p115.qr:
            raise Problem("二维码已过期", 404)
        image = qrcode.make(p115.qr.get("qrcode") or "https://115.com/scan/dg-" + p115.qr["uid"], image_factory=SvgPathImage)
        return Response(image.to_string(), media_type="image/svg+xml")

    @app.post("/api/v1/p115/login")
    async def p115_login(data: dict):
        return await p115.save_cookies(str(data.get("cookies", "")))

    @app.post("/api/v1/p115/logout")
    async def p115_logout():
        p115.client = None
        await blocking(vault.delete, "115-cookies")
        store.configure({"115_account_verified": None})
        return {"ok": True}

    dist = Path(frontend_dist) if frontend_dist is not None else Path(__file__).resolve().parents[2] / "frontend/dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist/"assets"), name="assets")
        @app.get("/{path:path}")
        async def index(path: str):
            if path.startswith("api/"):
                raise Problem("接口不存在", 404)
            return FileResponse(dist/"index.html")
    return app
