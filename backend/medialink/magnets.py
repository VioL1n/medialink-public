"""Bounded, metadata-only magnet previews. No cloud downloads or payload writes."""
import asyncio
import hashlib
import json
import shutil
import time
from contextlib import suppress
from pathlib import PurePosixPath
from tempfile import mkdtemp
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from .core import Problem, blocking, resource_key
from .curation import classify, kind_of
from .naming import file_id

MAX_FILES = 20000
PREVIEW_TTL = 86400
MAX_TRACKERS = 30
DHT_SAVE_INTERVAL = 600
# Many search results carry only a BTIH; well-known public trackers find peers
# much faster than DHT alone. Links' own trackers are still queried first.
DEFAULT_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "http://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.theoks.net:6969/announce",
    "udp://tracker.qu.ax:6969/announce",
    "udp://tracker.dler.org:6969/announce",
)


def safe_relative_path(path):
    if not isinstance(path, str) or not path or len(path) > 2000 or "\\" in path or "\x00" in path:
        raise Problem("磁力资源包含不安全文件路径", 422)
    parts = path.split("/")
    if len(parts) > 64 or any(part in {"", ".", ".."} for part in parts) or PurePosixPath(path).is_absolute():
        raise Problem("磁力资源包含不安全文件路径", 422)
    return path


def preview_snapshot(infohash, root, files):
    if not files or len(files) > MAX_FILES:
        raise Problem("磁力文件列表为空或超过 20,000 个文件", 422)
    items, seen = [], set()
    for entry in files:
        path = safe_relative_path(entry["path"])
        if path in seen or not isinstance(entry["size"], int) or entry["size"] < 0:
            raise Problem("磁力文件列表包含重复路径或无效大小", 422)
        seen.add(path)
        items.append({"id": file_id(path), "path": path, "size": entry["size"], "kind": kind_of(path)})
    # Suggest a default selection without ads, samples or unrelated files.
    curated = classify(items)
    for item in items:
        item.update(role=curated[item["path"]]["role"], keep=curated[item["path"]]["keep"],
                    reason=curated[item["path"]]["reason"])
    for path in seen:
        if any(str(parent) in seen for parent in PurePosixPath(path).parents if str(parent) != "."):
            raise Problem("磁力资源包含文件与目录路径冲突", 422)
    # Only multi-file torrents have a root that can be omitted by the cloud downloader.
    root = root if root and all(item["path"].startswith(root + "/") for item in items) else None
    data = {"infohash": infohash, "root": root, "files": items, "total_size": sum(item["size"] for item in items)}
    data["preview_id"] = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return data


def magnet_trackers(link):
    # Ignore web-seeds and external torrent URLs; fetch metadata through peers only.
    trackers = parse_qs(urlparse(link).query).get("tr", [])[:20]
    trackers = [url for url in trackers if urlparse(url).scheme in {"udp", "http", "https"} and urlparse(url).hostname]
    return list(dict.fromkeys(trackers + list(DEFAULT_TRACKERS)))[:MAX_TRACKERS]


def selection_signature(selection):
    return sorted(set(selection["selected_ids"])) if selection else None


class MagnetPreviews:
    def __init__(self, store, timeout=60, budget=180):
        # timeout bounds one request's wait; budget bounds the background
        # resolution, which outlives impatient requests so a retry resumes it.
        self.store, self.timeout, self.budget = store, timeout, budget
        self.slots = asyncio.Semaphore(3)
        self.engine_lock = asyncio.Lock()
        self.pending = {}
        self.engines = {}

    @staticmethod
    def cache_key(key):
        return "magnet-preview:" + key

    def cached(self, key):
        cached = self.store.cached_metadata(self.cache_key(key))
        return cached["data"] if cached and cached["expires"] > time.time() else None

    async def preview(self, link, wait=...):
        key = resource_key(link)
        if not key.startswith("magnet:"):
            raise Problem("请提供有效的磁力链接", 422)
        if result := self.cached(key):
            return result
        task = self.pending.get(key)
        if task is None:
            task = asyncio.create_task(self._resolve_and_cache(link, key))
            self.pending[key] = task
            task.add_done_callback(lambda done: self._finished(key, done))
        try:
            async with asyncio.timeout(self.timeout if wait is ... else wait):
                return await asyncio.shield(task)
        except TimeoutError:
            # Retry-After tells the client that asking again resumes this resolution.
            raise Problem("磁力列表仍在后台解析，可稍后重试继续等待，或直接确认入库", 504, {"Retry-After": "0"}) from None

    def _finished(self, key, task):
        if self.pending.get(key) is task:
            del self.pending[key]
        if not task.cancelled():
            task.exception()

    async def _resolve_and_cache(self, link, key):
        # Queueing for a slot does not consume the resolution budget.
        async with self.slots:
            try:
                async with asyncio.timeout(self.budget):
                    data = await self.resolve(link, key)
            except TimeoutError:
                raise Problem("磁力元数据解析超时，暂未找到可用节点，请重试或直接确认入库", 502) from None
            self.store.cache_metadata(self.cache_key(key), data, PREVIEW_TTL)
            return data

    async def close(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
        for engine in self.engines.values():
            self._retire(engine)
        self.engines.clear()

    def select(self, key, requested):
        preview = self.cached(key)
        if not preview or preview["preview_id"] != requested.preview_id:
            raise Problem("文件预览已失效，请重新解析后选择", 409)
        ids = set(requested.selected_ids)
        files = [item for item in preview["files"] if item["id"] in ids]
        if not ids or len(files) != len(ids) or not any(item["kind"] == "video" for item in files):
            raise Problem("请选择有效文件，并至少勾选一个视频", 422)
        return {"preview_id": preview["preview_id"], "root": preview["root"],
                "selected_ids": sorted(ids), "files": files, "total_count": len(preview["files"])}

    def _save_engine(self, engine, force=False):
        # Persist only DHT routing state, never proxy credentials or torrent tasks.
        if not force and time.time() - engine.get("saved", 0) < DHT_SAVE_INTERVAL:
            return
        engine["saved"] = time.time()
        with suppress(Exception):
            import libtorrent as lt

            flags = lt.save_state_flags_t.save_dht_state
            state = lt.write_session_params_buf(engine["session"].session_state(flags), flags)
            self.store.cache_metadata(engine["cache_key"], {"state": state.hex()}, PREVIEW_TTL)

    def _retire(self, engine):
        self._save_engine(engine, force=True)
        engine["session"].pause()

    def _proxy(self):
        settings = self.store.settings()
        return settings["proxy"] if settings["magnet_use_proxy"] else ""

    def _session(self, proxy_url, cache_key):
        # Runs in a worker thread: constructing a session binds sockets and
        # starts libtorrent's threads.
        import libtorrent as lt

        settings = {"enable_dht": True, "enable_lsd": False, "enable_upnp": False, "enable_natpmp": False,
                    "dht_bootstrap_nodes": "dht.libtorrent.org:25401,router.bittorrent.com:6881,router.utorrent.com:6881,dht.transmissionbt.com:6881",
                    "announce_to_all_trackers": True, "announce_to_all_tiers": True,
                    # Stay far below the service's descriptor limit and give up on dead peers quickly.
                    "connections_limit": 120, "connection_speed": 60, "torrent_connect_boost": 40,
                    "peer_connect_timeout": 7, "alert_mask": 0}
        proxy = urlparse(proxy_url)
        if proxy.hostname:
            types = {"socks5": lt.proxy_type_t.socks5, "socks5h": lt.proxy_type_t.socks5,
                     "http": lt.proxy_type_t.http}
            if proxy.scheme not in types:
                raise Problem("当前代理类型不支持磁力解析，可关闭磁力代理、重试或直接确认入库", 503)
            proxy_type = types[proxy.scheme]
            if proxy.username:
                proxy_type = lt.proxy_type_t.socks5_pw if proxy.scheme.startswith("socks5") else lt.proxy_type_t.http_pw
            settings.update(proxy_type=proxy_type, proxy_hostname=proxy.hostname, proxy_port=proxy.port or (1080 if proxy.scheme.startswith("socks5") else 80),
                            proxy_username=unquote(proxy.username or ""), proxy_password=unquote(proxy.password or ""),
                            proxy_hostnames=True, proxy_peer_connections=True, proxy_tracker_connections=True)
        params = lt.session_params(settings)
        cached = self.store.cached_metadata(cache_key)
        if cached and cached["expires"] > time.time():
            with suppress(ValueError, TypeError, RuntimeError, KeyError):
                state = lt.read_session_params(bytes.fromhex(cached["data"]["state"]), lt.save_state_flags_t.save_dht_state)
                params.dht_state = state.dht_state
        return lt.session(params)

    async def _engine(self):
        proxy_url = self._proxy()
        async with self.engine_lock:
            if engine := self.engines.get(proxy_url):
                engine["users"] += 1
                return engine
            # A settings change takes effect on the next preview. In-flight previews
            # keep their own session until their metadata task has been released.
            for key, old in list(self.engines.items()):
                if not old["users"]:
                    del self.engines[key]
                    await blocking(self._retire, old)
            cache_key = "magnet-dht:" + hashlib.sha256(proxy_url.encode()).hexdigest()
            engine = {"session": await blocking(self._session, proxy_url, cache_key), "users": 1,
                      "cache_key": cache_key, "saved": 0}
            self.engines[proxy_url] = engine
            return engine

    async def warmup(self):
        # Bootstrap while the service is idle, before a user opens the modal.
        # A failed warmup must not prevent the rest of the app from starting.
        with suppress(Exception):
            engine = await self._engine()
            engine["users"] -= 1

    async def resolve(self, link, key):
        import libtorrent as lt

        infohash = key.split(":", 1)[1]
        magnet = "magnet:?" + urlencode([("xt", "urn:btih:" + infohash)] + [("tr", url) for url in magnet_trackers(link)])
        engine, handle, directory = None, None, None
        try:
            engine = await self._engine()
            session = engine["session"]
            directory = await blocking(mkdtemp, prefix="medialink-metadata-")
            params = lt.parse_magnet_uri(magnet)
            params.save_path = directory
            # stop_when_ready can pause a magnet before metadata arrives in 2.1.1.
            # upload_mode plus zero file priorities prevents all payload downloads.
            params.flags |= lt.torrent_flags.upload_mode
            params.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
            params.file_priorities = [0] * (MAX_FILES + 1)
            handle = await blocking(session.add_torrent, params)
            while True:
                info = await blocking(handle.torrent_file)
                if info:
                    if str(info.info_hashes().v1) != infohash:
                        raise Problem("磁力元数据与链接的 BTIH 不符", 422)
                    storage = info.files()
                    if storage.num_files() > MAX_FILES:
                        raise Problem("磁力内容超过 20,000 个文件", 422)
                    files = []
                    for index in range(storage.num_files()):
                        flags = storage.file_flags(index)
                        if flags & lt.file_storage.flag_symlink:
                            raise Problem("磁力资源包含不支持的符号链接", 422)
                        if not flags & lt.file_storage.flag_pad_file:
                            files.append({"path": storage.file_path(index), "size": storage.file_size(index)})
                    return preview_snapshot(infohash, info.name(), files)
                status = await blocking(handle.status)
                if status.errc.value():
                    raise Problem("磁力元数据暂时无法解析，请重试或直接确认入库", 502)
                await asyncio.sleep(0.2)
        except Problem:
            raise
        except Exception:
            raise Problem("磁力解析暂时不可用，请重试或直接确认入库", 503) from None
        finally:
            try:
                if engine is not None:
                    try:
                        if handle is not None:
                            await blocking(engine["session"].remove_torrent, handle)
                    finally:
                        engine["users"] -= 1
                        await blocking(self._save_engine, engine)
            finally:
                if directory is not None:
                    await blocking(shutil.rmtree, directory, ignore_errors=True)
