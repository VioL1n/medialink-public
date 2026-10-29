import asyncio
import html
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from telethon import TelegramClient, errors, events, utils
from telethon.sessions import StringSession

from .core import Problem, blocking, resource_key


def resolution(text):
    match = re.search(r"\b(2160p|1080[pi]|720p|480p|4k|8k)\b", text, re.I)
    return match[1].upper() if match else None


def extract_resources(text, hidden_links=(), origin=None, published=None):
    text = html.unescape(text or "")
    links = re.findall(r"magnet:\?[^\s<>\"'\u3000]+|https?://(?:www\.)?(?:115\.com|115cdn\.com|anxia\.com)/s/[A-Za-z0-9]+(?:\?[^\s<>\"'\u3000]+)?", text)
    links.extend(hidden_links)
    password_match = re.search(r"(?:提取码|访问码|密码|口令)\s*[:：]?\s*([A-Za-z0-9]{4})", text)
    password = password_match[1] if password_match else ""
    title = next((line.strip() for line in text.splitlines() if line.strip()), "未命名资源")[:220]
    output = {}
    for link in links:
        link = html.unescape(link).rstrip("。，、；）)]}")
        try:
            key = resource_key(link)
        except Problem:
            continue
        share_password = parse_qs(urlparse(link).query).get("password", [password])[0] if key.startswith("share:") else ""
        output[key] = {"key": key, "title": title, "kind": "magnet" if key.startswith("magnet:") else "share",
                       "link": link, "password": share_password, "size": None, "resolution": resolution(text),
                       "published": published, "seeders": None, "sources": [origin] if origin else []}
    return list(output.values())


def merge_results(results):
    combined = {}
    for result in results:
        if result["key"] not in combined:
            combined[result["key"]] = {**result, "sources": list(result["sources"])}
        else:
            existing = combined[result["key"]]
            for source in result["sources"]:
                if source not in existing["sources"]:
                    existing["sources"].append(source)
            for field in ("size", "seeders", "resolution", "password"):
                if not existing.get(field) and result.get(field):
                    existing[field] = result[field]
            if result.get("matched_queries"):
                existing["matched_queries"] = list(dict.fromkeys(existing.get("matched_queries", [])+result["matched_queries"]))
    return list(combined.values())


class Nyaa:
    def __init__(self, settings):
        self.settings = settings
        self.cache = {}
        self.lock = asyncio.Lock()
        self.last_request = 0

    @staticmethod
    def parse(body, base):
        soup = BeautifulSoup(body, "html.parser")
        table = soup.select_one("table.torrent-list")
        if table is None:
            heading = soup.select_one("div.container h3")
            statistics = soup.select_one("div.container .pagination-page-info")
            if (soup.select_one('form input[name="q"]')
                    and heading and heading.get_text(" ", strip=True).casefold() == "no results found"
                    and statistics and re.search(r"\bDisplaying\s+results\s+0\s*-\s*0\s+out\s+of\s+0\s+results\b",
                                                statistics.get_text(" ", strip=True), re.I)):
                return []
            raise Problem("Nyaa 返回了无法识别的页面", 502)
        results = []
        for row in table.select("tbody tr"):
            cells = row.select("td")
            category = row.select_one('a[href*="c="]')
            if category and parse_qs(urlparse(category.get("href", "")).query).get("c", [""])[0].split("_")[0] not in {"1", "4"}:
                continue
            title_anchor = row.select_one('a[href^="/view/"]:not(.comments)')
            magnet = row.select_one('a[href^="magnet:"]')
            if not title_anchor or not magnet or len(cells) < 8:
                continue
            title, link = title_anchor.get_text(strip=True), magnet.get("href", "")
            try:
                key = resource_key(link)
            except Problem:
                continue
            epoch = cells[4].get("data-timestamp")
            published = datetime.fromtimestamp(int(epoch), timezone.utc).isoformat() if epoch and epoch.isdigit() else None
            results.append({"key": key, "title": title, "kind": "magnet", "link": link, "password": "",
                            "size": cells[3].get_text(strip=True), "seeders": cells[5].get_text(strip=True),
                            "resolution": resolution(title), "published": published,
                            "sources": [{"provider": "nyaa", "name": "Nyaa", "url": urljoin(base, title_anchor["href"])}]})
        return results

    async def search(self, query):
        settings = self.settings()
        cache_key = (query, settings["nyaa_url"], settings["proxy"])
        cached = self.cache.get(cache_key)
        if cached and cached[0] > time.time():
            return cached[1]
        async with self.lock:
            await asyncio.sleep(max(0, 2-(time.time()-self.last_request)))
            self.last_request = time.time()
            try:
                async with httpx.AsyncClient(timeout=18, proxy=settings["proxy"] or None, trust_env=False,
                                             follow_redirects=True) as client:
                    response = await client.get(settings["nyaa_url"], params={"q": query, "c": "0_0", "f": "0"},
                                                headers={"User-Agent": "MediaLink/0.1"})
                    response.raise_for_status()
                results = self.parse(response.text, settings["nyaa_url"])
            except httpx.HTTPError:
                raise Problem("Nyaa 暂时无法访问，可在设置中配置代理", 502) from None
            if len(self.cache) > 100:
                self.cache.clear()
            self.cache[cache_key] = (time.time()+120, results)
            return results


class Telegram:
    def __init__(self, store, vault):
        self.store, self.vault = store, vault
        self.client = None
        self.phone = None
        self.phone_hash = None
        self.lock = asyncio.Lock()
        self.stopped = False

    async def connect(self):
        if self.client and self.client.is_connected():
            return self.client
        settings = self.store.settings()
        api_hash = await blocking(self.vault.get, "telegram-api-hash")
        if not settings["telegram_api_id"] or not api_hash:
            raise Problem("请先配置 Telegram API ID 和 API Hash", 409)
        session = await blocking(self.vault.get, "telegram-session")
        proxy = None
        if settings["proxy"]:
            url = urlparse(settings["proxy"])
            proxy = {"proxy_type": url.scheme.replace("socks5h", "socks5"), "addr": url.hostname,
                     "port": url.port, "username": url.username, "password": url.password, "rdns": True}
        self.client = TelegramClient(StringSession(session or ""), int(settings["telegram_api_id"]), api_hash,
                                     proxy=proxy, flood_sleep_threshold=0, receive_updates=True)
        self.client.add_event_handler(self.on_message, events.NewMessage)
        self.client.add_event_handler(self.on_message, events.MessageEdited)
        self.client.add_event_handler(self.on_delete, events.MessageDeleted)
        try:
            await asyncio.wait_for(self.client.connect(), timeout=20)
            if await self.client.is_user_authorized():
                await self.client.get_dialogs()
        except Exception:
            self.client = None
            raise Problem("Telegram 连接失败，请检查代理设置", 502) from None
        return self.client

    async def authorized(self):
        try:
            client = await self.connect()
            return await client.is_user_authorized()
        except Problem:
            return False

    async def send_code(self, phone):
        async with self.lock:
            client = await self.connect()
            try:
                sent = await client.send_code_request(phone)
                self.phone, self.phone_hash = phone, sent.phone_code_hash
                return {"status": "code_sent"}
            except errors.FloodWaitError as e:
                raise Problem("Telegram 要求等待 %s 秒后再登录" % e.seconds, 429) from None
            except Exception:
                raise Problem("无法发送登录验证码，请检查手机号", 422) from None

    async def sign_in(self, code="", password=""):
        async with self.lock:
            client = await self.connect()
            try:
                if password:
                    await client.sign_in(password=password)
                else:
                    if not self.phone_hash:
                        raise Problem("请先发送验证码", 409)
                    await client.sign_in(phone=self.phone, code=code, phone_code_hash=self.phone_hash)
                await blocking(self.vault.set, "telegram-session", client.session.save())
                self.phone, self.phone_hash = None, None
                return {"status": "connected"}
            except errors.SessionPasswordNeededError:
                return {"status": "password_required"}
            except Problem:
                raise
            except Exception:
                raise Problem("验证码或两步验证密码无效", 422) from None

    async def reset(self):
        if self.client:
            await self.client.disconnect()
        self.client = None

    async def logout(self):
        if self.client:
            try:
                await self.client.log_out()
            except Exception:
                await self.client.disconnect()
        self.client = None
        await blocking(self.vault.delete, "telegram-session")

    async def dialogs(self):
        client = await self.connect()
        if not await client.is_user_authorized():
            raise Problem("请先登录 Telegram", 409)
        result = []
        async for d in client.iter_dialogs():
            if d.is_channel:
                result.append({"id": str(d.id), "title": d.name, "username": getattr(d.entity, "username", None)})
        return result

    async def add_channel(self, peer):
        client = await self.connect()
        if not await client.is_user_authorized():
            raise Problem("请先登录 Telegram", 409)
        if peer.startswith("https://t.me/"):
            peer = peer.removeprefix("https://t.me/").strip("/")
        if peer.startswith("+") or "joinchat" in peer:
            raise Problem("请先在 Telegram 中加入频道，再从已加入列表选择", 422)
        try:
            await client.get_dialogs()
            entity = await client.get_entity(int(peer) if re.fullmatch(r"-?\d+", peer) else peer)
            if not hasattr(entity, "broadcast"):
                raise Problem("仅支持频道或超级群组", 422)
            # This read verifies access without joining anything.
            await client.get_messages(entity, limit=1)
        except Problem:
            raise
        except Exception:
            raise Problem("该频道不存在或当前账号无权读取", 422) from None
        channel_id = str(utils.get_peer_id(entity))
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO channels(id,peer,title) VALUES (?,?,?)",
                       (channel_id, channel_id, entity.title))
        return self.store.channel(channel_id)

    async def save_message(self, channel, message):
        links = []
        for entity in message.entities or []:
            url = getattr(entity, "url", None)
            if url:
                links.append(url)
        for row in getattr(message.reply_markup, "rows", []) or []:
            links.extend(button.url for button in row.buttons if getattr(button, "url", None))
        username = getattr(message.chat, "username", None)
        chat_path = username or "c/" + channel["id"].removeprefix("-100")
        origin = {"provider": "telegram", "name": channel["title"], "url": f"https://t.me/{chat_path}/{message.id}"}
        published = message.date.isoformat()
        body = message.raw_text or ""
        resources = extract_resources(body, links, origin, published)
        revision = (message.edit_date or message.date).timestamp()
        await blocking(self.store.put_message, channel["id"], message.id, body, resources, published, revision)

    async def on_message(self, event):
        try:
            channel = self.store.channel(str(event.chat_id))
            if channel["enabled"] and channel["mode"] == "api":
                await self.save_message(channel, event.message)
        except Problem:
            pass

    async def on_delete(self, event):
        try:
            channel = self.store.channel(str(event.chat_id))
            if channel["enabled"] and channel["mode"] == "api":
                for message_id in event.deleted_ids:
                    await blocking(self.store.put_message, channel["id"], message_id, None, [], None)
        except Problem:
            pass

    async def sync_channel(self, channel):
        client = await self.connect()
        entity = await client.get_entity(int(channel["peer"]))
        latest = await client.get_messages(entity, limit=1)
        if not latest:
            self.store.channel(channel["id"], complete=1, status="历史同步完成")
            return
        if not channel["highwater"]:
            channel = self.store.channel(channel["id"], highwater=latest[0].id)
        else:
            # Ascending replay means each committed highwater is safe after a crash.
            async for message in client.iter_messages(entity, min_id=channel["highwater"], reverse=True, limit=200):
                if not self.store.channel(channel["id"])["enabled"]:
                    return
                await self.save_message(channel, message)
                channel = self.store.channel(channel["id"], highwater=message.id)
        if channel["complete"]:
            self.store.channel(channel["id"], status="历史同步完成 · 接收新消息", error=None)
            return
        self.store.channel(channel["id"], status="正在回溯全部历史", error=None)
        batch = await client.get_messages(entity, offset_id=channel["cursor"] or channel["highwater"]+1, limit=100)
        for message in batch:
            if not self.store.channel(channel["id"])["enabled"]:
                return
            await self.save_message(channel, message)
        if batch:
            self.store.channel(channel["id"], cursor=batch[-1].id)
        if len(batch) < 100:
            self.store.channel(channel["id"], complete=1, status="历史同步完成 · 接收新消息")

    async def run(self):
        while not self.stopped:
            for channel in self.store.channels():
                if channel["mode"] != "api" or not channel["enabled"] or channel["wait_until"] > time.time():
                    continue
                try:
                    if not await self.authorized():
                        self.store.channel(channel["id"], status="等待 Telegram 登录")
                        break
                    await self.sync_channel(channel)
                    self.store.channel(channel["id"], last_synced=time.time())
                except errors.FloodWaitError as e:
                    self.store.channel(channel["id"], wait_until=time.time()+e.seconds, status="Telegram 限流等待",
                                       error=f"{e.seconds} 秒后自动继续")
                except (errors.ChannelPrivateError, errors.UserDeactivatedError, errors.AuthKeyUnregisteredError):
                    self.store.channel(channel["id"], status="需要处理", error="账号失效或频道不可访问", wait_until=time.time()+300)
                except Exception:
                    if any(c["id"] == channel["id"] for c in self.store.channels()):
                        self.store.channel(channel["id"], status="连接中断，稍后重试", error="请检查登录与代理", wait_until=time.time()+60)
                await asyncio.sleep(1)
            await asyncio.sleep(3 if any(c["mode"] == "api" and not c["complete"] and c["enabled"]
                                       for c in self.store.channels()) else 20)

    async def search(self, query):
        messages = await blocking(self.store.search_messages, query)
        return merge_results([r for m in messages for r in json.loads(m["resources"])])
