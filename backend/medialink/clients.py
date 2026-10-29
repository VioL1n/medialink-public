import asyncio
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlparse

import httpx

from .core import Problem, blocking, resource_key


def readonly_database(path):
    # Share the whole live SQLite directory, including WAL/SHM. Never use immutable=1.
    uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro"
    return closing(sqlite3.connect(uri, uri=True))


class RemoteFailure(Problem):
    def __init__(self, message, uncertain=False):
        super().__init__(message, 502)
        self.uncertain = uncertain


class OpenList:
    def __init__(self, settings, vault):
        self.settings, self.vault = settings, vault

    def token(self):
        explicit = self.vault.get("openlist-token")
        if explicit:
            return explicit
        try:
            with readonly_database(self.settings()["openlist_db"]) as db:
                row = db.execute("SELECT value FROM x_setting_items WHERE key='token'").fetchone()
            if row and row[0]:
                return row[0]
        except sqlite3.Error:
            pass
        raise Problem("无法读取 OpenList 令牌，请在设置中配置", 503)

    async def request(self, method, endpoint, payload=None, params=None, *, read_timeout=25):
        token = await blocking(self.token)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(25, read=read_timeout), trust_env=False) as client:
                response = await client.request(method, self.settings()["openlist_url"].rstrip("/") + endpoint,
                                                json=payload, params=params, headers={"Authorization": token})
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise RemoteFailure("OpenList 请求未得到可靠响应", uncertain=method != "GET") from None
        if data.get("code") != 200:
            # Upstream messages may contain links or credentials; do not forward them.
            raise RemoteFailure("OpenList 拒绝请求（业务码 %s）" % data.get("code"))
        return data.get("data")

    async def entries(self, path, refresh=True):
        data = await self.request("POST", "/api/fs/list", {"path": path, "refresh": refresh, "per_page": 0})
        return (data or {}).get("content") or []

    async def get(self, path):
        return await self.request("POST", "/api/fs/get", {"path": path})

    async def mkdir(self, path):
        try:
            await self.get(path)
            return
        except RemoteFailure as e:
            if e.uncertain:
                raise
        await self.request("POST", "/api/fs/mkdir", {"path": path})
        for _ in range(10):
            try:
                await self.get(path)
                return
            except RemoteFailure:
                await asyncio.sleep(0.5)
        raise RemoteFailure("新建目录暂未可见，请稍后重试")

    async def offline(self, link, path):
        data = await self.request("POST", "/api/fs/add_offline_download", {
            "urls": [link], "path": path, "tool": "115 Open", "delete_policy": "delete_never"})
        first = data[0] if isinstance(data, list) and data else data
        if isinstance(first, dict):
            return first.get("id") or first.get("task_id")
        return first if isinstance(first, str) else None

    async def remote_tasks(self):
        output = []
        for bucket in ("undone", "done"):
            data = await self.request("GET", "/api/task/offline_download/" + bucket)
            if isinstance(data, dict):
                data = data.get("content") or data.get("tasks") or []
            output.extend({**t, "bucket": bucket} for t in (data or []) if isinstance(t, dict))
        return output

    async def move(self, src, dst, names):
        await self.request("POST", "/api/fs/move", {
            "src_dir": src, "dst_dir": dst, "names": names, "overwrite": False}, read_timeout=120)

    async def rename(self, path, name):
        await self.request("POST", "/api/fs/rename", {"path": path, "name": name, "overwrite": False})

    async def scan_status(self):
        return await self.request("GET", "/api/admin/scan/progress") or {}

    async def scan(self, path):
        path = await self.resolve_strm_path(path)
        await self.request("POST", "/api/admin/scan/start", {"path": path, "limit": 2})
        return path

    async def resolve_strm_path(self, path):
        # Recent OpenList versions flatten a single STRM source's virtual root.
        # Probe before scanning: the scan API can accept a nonexistent path.
        try:
            await self.get(path)
            return path
        except RemoteFailure:
            pass
        def candidate():
            try:
                with readonly_database(self.settings()["openlist_db"]) as db:
                    rows = db.execute("SELECT mount_path,addition FROM x_storages WHERE driver='Strm'").fetchall()
                for mount, raw in sorted(rows, key=lambda row: len(row[0]), reverse=True):
                    sources = [line.strip() for line in json.loads(raw).get("paths", "").splitlines() if line.strip()]
                    if len(sources) != 1:
                        continue
                    alias = sources[0].split(":", 1)[0] if ":" in sources[0] else PurePosixPath(sources[0]).name
                    prefix = PurePosixPath(mount) / alias
                    try:
                        relative = PurePosixPath(path).relative_to(prefix)
                    except ValueError:
                        continue
                    return str(PurePosixPath(mount) / relative)
            except (sqlite3.Error, ValueError, TypeError):
                pass
            return None
        resolved = await blocking(candidate)
        if not resolved:
            raise Problem("STRM 目录不可访问，请检查 OpenList 路径映射", 409)
        await self.get(resolved)
        return resolved

    async def cancel(self, task_id):
        await self.request("POST", "/api/task/offline_download/cancel", params={"tid": task_id})

    async def storage_identity(self):
        # The open driver's token is only read, never refreshed by this application.
        try:
            with readonly_database(self.settings()["openlist_db"]) as db:
                row = db.execute("SELECT addition FROM x_storages WHERE mount_path=? AND driver='115 Open'",
                                 (self.settings()["mount_root"],)).fetchone()
            addition = json.loads(row[0])
            access_token = addition["access_token"]
            root_id = str(addition.get("root_folder_id", "0"))
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                response = await client.get("https://proapi.115.com/open/user/info",
                                            headers={"Authorization": "Bearer " + access_token})
                data = response.json()
            if data.get("state") and (data.get("data") or {}).get("user_id"):
                return {"user_id": str(data["data"]["user_id"]), "root_id": root_id}
        except (sqlite3.Error, ValueError, KeyError, TypeError, httpx.HTTPError):
            pass
        raise Problem("无法验证 OpenList 的 115 账号，请确认其授权有效", 503)

    async def native_status(self, infohash, staging):
        """Read-only reconciliation against 115 itself, scoped to this task's unique folder."""
        try:
            with readonly_database(self.settings()["openlist_db"]) as db:
                row = db.execute("SELECT addition FROM x_storages WHERE mount_path=? AND driver='115 Open'",
                                 (self.settings()["mount_root"],)).fetchone()
            addition = json.loads(row[0])
            if str(addition.get("root_folder_id", "0")) != "0":
                return None
            access_token = addition["access_token"]
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            return None
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            async def read(endpoint, params):
                try:
                    response = await client.get("https://proapi.115.com"+endpoint, params=params,
                                                headers={"Authorization": "Bearer "+access_token})
                    response.raise_for_status()
                    result = response.json()
                except (httpx.HTTPError, ValueError):
                    raise RemoteFailure("115 任务核对暂时不可用") from None
                if not result.get("state"):
                    raise Problem("115 Open 授权失效或目录不可访问，请在 OpenList 检查", 409)
                return result.get("data") or {}
            relative = "/" + str(PurePosixPath(staging).relative_to(self.settings()["mount_root"]))
            folder = await read("/open/folder/get_info", {"path": relative})
            if folder.get("file_name") != PurePosixPath(staging).name:
                return None
            folder_id = str(folder.get("file_id"))
            for page in range(1, 21):
                data = await read("/open/offline/get_task_list", {"page": page})
                for task in data.get("tasks", []):
                    if str(task.get("info_hash", "")).lower() == infohash and str(task.get("wp_path_id")) == folder_id:
                        return {"status": int(task.get("status", -1)), "progress": float(task.get("percentDone", 0)),
                                "size": int(task.get("size", 0)), "folder_id": folder_id,
                                "file_id": str(task.get("file_id", ""))}
                if page >= int(data.get("page_count", 1)):
                    break
                await asyncio.sleep(0.3)
        return None


class P115:
    def __init__(self, vault, settings, openlist):
        self.vault, self.settings, self.openlist = vault, settings, openlist
        self.client = None
        self.qr = None
        self.qr_cookies = None
        self.qr_lock = asyncio.Lock()

    async def ready(self):
        from p115client import P115Client
        cookies = await blocking(self.vault.get, "115-cookies")
        if not cookies:
            raise Problem("请先在设置中登录 115", 409)
        if self.client is None:
            # Never automatically refresh OpenList's OAuth token or launch a blocking console login.
            self.client = P115Client(cookies)
        return self.client

    async def call(self, method, *args, **kwargs):
        from p115client import check_response
        client = await self.ready()
        try:
            result = await asyncio.wait_for(getattr(client, method)(*args, async_=True, timeout=25, **kwargs), timeout=35)
            return check_response(result)
        except Exception:
            raise RemoteFailure("115 操作失败，请检查登录、提取码或分享有效性", uncertain=method.startswith("share_receive")) from None

    async def save_cookies(self, cookies):
        from p115client import P115Client
        try:
            client = P115Client(cookies)
            # user_info() is a public profile lookup, defaulting to UID 11500.
            # Authenticate the actual cookie session before comparing its owner.
            logged_in = await asyncio.wait_for(client.login_status(async_=True, timeout=20), timeout=25)
            if not logged_in:
                raise Problem("115 登录会话已失效，请重新扫码", 422)
            uid = str(client.user_id)
        except Problem:
            raise
        except TimeoutError:
            raise Problem("115 登录验证超时，请点击继续验证", 503) from None
        except Exception:
            raise Problem("115 登录验证失败，请重新扫码或稍后继续验证", 422) from None
        identity = await self.openlist.storage_identity()
        if uid != identity["user_id"]:
            raise Problem("此 115 账号与 OpenList 挂载账号不同，请切换账号", 422)
        if identity["root_id"] != "0":
            raise Problem("当前版本要求 OpenList 的 /115 挂载网盘根目录", 422)
        await blocking(self.vault.set, "115-cookies", cookies)
        self.client = client
        self.settings_store.configure({"115_account_verified": uid})
        return {"verified": True}

    async def qr_start(self):
        from p115client import P115Client
        async with self.qr_lock:
            try:
                data = await asyncio.wait_for(P115Client.login_qrcode_token(async_=True, timeout=20), timeout=25)
                self.check_qr_response(data)
                qr = data["data"]
                if not all(k in qr for k in ("uid", "time", "sign")):
                    raise ValueError()
                self.qr, self.qr_cookies = qr, None
                return {"url": "/api/v1/p115/qr/image"}
            except Problem:
                raise
            except Exception:
                raise Problem("无法获取 115 登录二维码，请稍后重试", 502) from None

    @staticmethod
    def check_qr_response(data):
        if not isinstance(data, dict):
            raise Problem("115 扫码接口返回异常，请重新获取二维码", 502)
        if data.get("state", True):
            return
        message = str(data.get("message") or data.get("msg") or data.get("error") or "")
        if "IP" in message.upper() and "异常" in message:
            raise Problem("115 拒绝登录：IP 登录异常，请稍后在 115 官方客户端确认账号状态", 409)
        code = str(data.get("code") or data.get("errno") or "")
        suffix = "（业务码 " + code + "）" if code.isdigit() and len(code) <= 8 else ""
        raise Problem("115 拒绝扫码登录" + suffix + "，请重新获取二维码", 409)

    async def qr_poll(self):
        from p115client import P115Client
        async with self.qr_lock:
            if not self.qr:
                if self.client:
                    return {"status": "connected"}
                raise Problem("请重新获取二维码", 400)
            try:
                if self.qr_cookies is None:
                    data = await asyncio.wait_for(P115Client.login_qrcode_scan_status(
                        {k: self.qr[k] for k in ("uid", "time", "sign")}, async_=True, timeout=20), timeout=25)
                    self.check_qr_response(data)
                    status = int(data.get("data", {}).get("status", 0))
                    if status != 2:
                        return {"status": {0: "waiting", 1: "scanned", -1: "expired", -2: "canceled"}.get(status, "waiting")}
                    result = await asyncio.wait_for(P115Client.login_qrcode_scan_result(
                        self.qr["uid"], app="web", async_=True, timeout=20), timeout=25)
                    self.check_qr_response(result)
                    cookies = result.get("data", {}).get("cookie")
                    if not isinstance(cookies, dict) or not all(cookies.get(k) for k in ("UID", "CID", "SEID")):
                        raise Problem("115 未返回完整登录信息，请重新扫码", 502)
                    self.qr_cookies = "; ".join(str(k) + "=" + str(v) for k, v in cookies.items())
                await self.save_cookies(self.qr_cookies)
                self.qr, self.qr_cookies = None, None
                return {"status": "connected"}
            except Problem:
                raise
            except Exception:
                raise Problem("115 扫码验证暂时失败，请点击继续验证", 503) from None

    async def share_entries(self, link, password="", folder="0"):
        code = resource_key(link).split(":", 1)[1]
        password = password or parse_qs(urlparse(link).query).get("password", [""])[0]
        output, offset = [], 0
        while True:
            result = await self.call("share_snap", {"share_code": code, "receive_code": password,
                                                    "cid": folder, "limit": 100, "offset": offset})
            data = result.get("data") or {}
            items = data.get("list") or []
            output.extend({"id": str(t.get("fid") or t.get("cid")), "name": t.get("n"),
                           "is_dir": "fid" not in t, "size": int(t.get("s") or 0)} for t in items)
            offset += len(items)
            if not items or offset >= int(data.get("count", len(output))):
                return output
            await asyncio.sleep(1)

    async def folder_id(self, openlist_path):
        identity = await self.openlist.storage_identity()
        configured = self.settings().get("115_account_verified")
        if not configured or configured != identity["user_id"]:
            raise Problem("115 账号尚未验证或 OpenList 账号已变更，请重新登录 115", 409)
        root = self.settings()["mount_root"]
        relative = "/" + str(PurePosixPath(openlist_path).relative_to(root))
        result = await self.call("fs_dir_getid", relative)
        value = result.get("id") or result.get("data", {}).get("id")
        if not value:
            raise Problem("无法定位 115 目标目录", 502)
        return str(value)

    async def share_tree(self, link, password, selected):
        files, pending, seen = [], [], set()
        for entry in selected:
            if entry["is_dir"]:
                pending.append((entry["id"], entry["name"]))
            else:
                files.append({"path": entry["name"], "size": entry["size"]})
        while pending:
            folder, prefix = pending.pop()
            if folder in seen:
                raise Problem("分享目录结构异常", 422)
            seen.add(folder)
            entries = await self.share_entries(link, password, folder)
            for entry in entries:
                path = str(PurePosixPath(prefix) / entry["name"])
                if entry["is_dir"]:
                    pending.append((entry["id"], path))
                else:
                    files.append({"path": path, "size": entry["size"]})
            if len(files)+len(seen)+len(pending) > 20000:
                raise Problem("分享内容超过单次入库限制，请选择较小的目录", 422)
            await asyncio.sleep(0.5)
        return files

    async def receive(self, link, password, ids, folder_id):
        password = password or parse_qs(urlparse(link).query).get("password", [""])[0]
        await self.call("share_receive", {"share_code": resource_key(link).split(":", 1)[1],
                                         "receive_code": password, "file_id": ",".join(ids), "cid": folder_id})


async def refresh_emby(settings, vault):
    key = await blocking(vault.get, "emby-token")
    key = key or await blocking(vault.get, os.environ.get("USER", Path.home().name), settings["emby_keychain_service"])
    if not key:
        raise Problem("Emby API 密钥不可用，请在设置中配置", 503)
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        try:
            response = await client.post(settings["emby_url"].rstrip("/") + "/Library/Refresh", headers={"X-Emby-Token": key})
            response.raise_for_status()
        except httpx.HTTPError:
            raise RemoteFailure("Emby 刷新通知失败，文件与 STRM 已保留") from None
