import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlparse

DATA = Path(os.environ.get("MEDIALINK_DATA", str(Path.home() / "Library/Application Support/MediaLink")))
SERVICE = "asia.viol1n.medialink"
SEARCH_SOURCE_IDS = ("knaben", "limetorrents", "nyaa", "telegram")
DEFAULTS = {
    "openlist_url": "http://127.0.0.1:5244", "openlist_db": "/opt/homebrew/bin/data/data.db",
    "emby_url": "http://127.0.0.1:8096", "emby_keychain_service": "asia.viol1n.openlist-strm-sync.emby",
    "media_root": "/115/infuse", "strm_root": "/115Strm/infuse", "staging_root": "/115/MediaLinkStaging",
    "strm_local": str(Path.home() / "Applications/openlist/strm"), "mount_root": "/115",
    "last_target": "/115/infuse", "nyaa_url": "https://nyaa.si", "proxy": "", "telegram_api_id": "",
    "knaben_url": "https://api.knaben.org", "limetorrents_url": "https://www.limetorrents.fun",
    "mikan_url": "https://mikanani.me", "anime_target": "",
    "yts_url": "https://yts.gg", "eztv_url": "https://eztvx.to",
    "poll_seconds": 30,
    "public_access_enabled": False, "public_access_hosts": "",
    "enabled_search_sources": list(SEARCH_SOURCE_IDS), "default_use_tmdb": True,
    "llm_enabled": False, "llm_base_url": "", "llm_model": "", "llm_use_proxy": False,
    "magnet_use_proxy": False,
}


class Problem(Exception):
    def __init__(self, message, code=400, headers=None):
        self.message, self.code, self.headers = message, code, headers
        super().__init__(message)


class Vault:
    """Native installs use Keychain; containers explicitly select the encrypted file vault."""
    _key_lock = threading.Lock()

    def __init__(self):
        backend = os.environ.get("MEDIALINK_VAULT_BACKEND", "keychain")
        if backend not in {"keychain", "file"}:
            raise Problem("不支持的凭据存储方式", 503)
        self.files = None
        if backend == "file":
            from .file_vault import FileVault
            self.files = FileVault(Path(os.environ.get("MEDIALINK_VAULT_DIR", str(DATA / "vault"))))

    def get(self, name, service=SERVICE):
        if self.files is not None:
            return self.files.get(service, name)
        result = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", name, "-s", service, "-w"],
                                capture_output=True, text=True)
        return result.stdout.rstrip("\n") if result.returncode == 0 else None

    def set(self, name, value):
        if self.files is not None:
            return self.files.set(SERVICE, name, value)
        result = subprocess.run(["/usr/bin/security", "add-generic-password", "-U", "-a", name, "-s", SERVICE,
                                 "-w", value], capture_output=True)
        if result.returncode:
            raise Problem("无法保存到 macOS Keychain", 503)

    def delete(self, name):
        if self.files is not None:
            return self.files.delete(SERVICE, name)
        subprocess.run(["/usr/bin/security", "delete-generic-password", "-a", name, "-s", SERVICE], capture_output=True)

    def seal(self, value):
        from cryptography.fernet import Fernet
        with self._key_lock:
            key = self.get("data-key")
            if not key:
                key = Fernet.generate_key().decode()
                self.set("data-key", key)
        return Fernet(key.encode()).encrypt(value.encode()).decode()

    def unseal(self, value):
        from cryptography.fernet import Fernet
        key = self.get("data-key")
        if not key:
            raise Problem("凭据存储中的数据密钥不可用", 503)
        return Fernet(key.encode()).decrypt(value.encode()).decode()


def resource_key(link):
    if link.lower().startswith("magnet:?"):
        values = parse_qs(urlparse(link).query).get("xt", [])
        value = next((v[9:] for v in values if v.lower().startswith("urn:btih:")), "")
        if re.fullmatch(r"[a-fA-F0-9]{40}", value):
            return "magnet:" + value.lower()
        if re.fullmatch(r"[a-zA-Z2-7]{32}", value):
            return "magnet:" + base64.b32decode(value.upper()).hex()
        raise Problem("磁力链接缺少有效的 BTIH", 422)
    parsed = urlparse(link)
    if parsed.hostname not in {"115.com", "115cdn.com", "anxia.com", "www.115.com", "www.115cdn.com"}:
        raise Problem("仅支持磁力链接和 115 分享", 422)
    match = re.fullmatch(r"/s/([a-zA-Z0-9]+)(?:/)?", parsed.path)
    if not match:
        raise Problem("115 分享链接格式无效", 422)
    return "share:" + match[1]


def target_path(path, root):
    if not isinstance(path, str) or "\x00" in path:
        raise Problem("目录无效", 422)
    p = PurePosixPath(path)
    if not p.is_absolute() or ".." in p.parts or not p.is_relative_to(PurePosixPath(root)):
        raise Problem("目标目录必须位于媒体根目录内", 422)
    return str(p)


def tokenize(text):
    import jieba
    return " ".join(t.lower() for t in jieba.cut_for_search(text) if t.strip() and re.search(r"\w", t))


class Store:
    def __init__(self, path=DATA / "medialink.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS channels(id TEXT PRIMARY KEY,peer TEXT NOT NULL,title TEXT NOT NULL,
              enabled INTEGER DEFAULT 1,cursor INTEGER DEFAULT 0,highwater INTEGER DEFAULT 0,
              count INTEGER DEFAULT 0,complete INTEGER DEFAULT 0,status TEXT DEFAULT '等待同步',
              error TEXT,wait_until REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS messages(channel_id TEXT,message_id INTEGER,body TEXT NOT NULL,
              tokens TEXT NOT NULL,published TEXT,resources TEXT NOT NULL,PRIMARY KEY(channel_id,message_id));
            CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(tokens,channel_id UNINDEXED,message_id UNINDEXED);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,resource_key TEXT NOT NULL,target TEXT NOT NULL,
              payload TEXT NOT NULL,state TEXT NOT NULL,progress REAL DEFAULT 0,error TEXT,
              created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(resource_key,target));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,state TEXT,
              message TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS sessions(digest TEXT PRIMARY KEY,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS deleted_messages(channel_id TEXT,message_id INTEGER,
              PRIMARY KEY(channel_id,message_id));
            CREATE TABLE IF NOT EXISTS metadata_cache(key TEXT PRIMARY KEY,data TEXT NOT NULL,
              updated REAL NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY,bangumi_id INTEGER NOT NULL,
              subgroup_id INTEGER,subgroup_name TEXT,title TEXT NOT NULL,poster TEXT,media_type TEXT NOT NULL,
              media_id INTEGER NOT NULL,season_number INTEGER,target TEXT NOT NULL,rules TEXT NOT NULL,
              backfill INTEGER DEFAULT 1,initialized INTEGER DEFAULT 0,enabled INTEGER DEFAULT 1,
              created REAL NOT NULL,checked REAL DEFAULT 0,next_check REAL DEFAULT 0,error TEXT,
              failures INTEGER DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS subscription_source
              ON subscriptions(bangumi_id,ifnull(subgroup_id,-1));
            CREATE TABLE IF NOT EXISTS subscription_episodes(subscription_id TEXT NOT NULL,episode INTEGER NOT NULL,
              resource_key TEXT,title TEXT,task_id TEXT,created REAL NOT NULL,PRIMARY KEY(subscription_id,episode));
            CREATE TABLE IF NOT EXISTS home_releases(hash TEXT PRIMARY KEY,item TEXT NOT NULL,published REAL NOT NULL,
              data TEXT NOT NULL);
            """)
            if "revision" not in {row[1] for row in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN revision REAL DEFAULT 0")
            columns = {row[1] for row in db.execute("PRAGMA table_info(channels)")}
            for name, definition in {
                "mode": "TEXT NOT NULL DEFAULT 'api'", "last_synced": "REAL DEFAULT 0",
                "refresh_before": "INTEGER DEFAULT 0", "refresh_highwater": "INTEGER DEFAULT 0",
                "refresh_at": "REAL DEFAULT 0", "failures": "INTEGER DEFAULT 0",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE channels ADD COLUMN {name} {definition}")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def settings(self):
        defaults = dict(DEFAULTS)
        # Environment values are defaults only; a user's saved settings take precedence.
        for key in ("openlist_url", "openlist_db", "emby_url", "media_root", "strm_root", "staging_root",
                    "strm_local", "mount_root", "last_target", "proxy"):
            variable = "MEDIALINK_" + key.upper()
            if variable in os.environ:
                defaults[key] = os.environ[variable]
        if "MEDIALINK_ENABLED_SEARCH_SOURCES" in os.environ:
            enabled = list(dict.fromkeys(part.strip() for part in
                                        os.environ["MEDIALINK_ENABLED_SEARCH_SOURCES"].split(",") if part.strip()))
            if any(key not in SEARCH_SOURCE_IDS for key in enabled):
                raise Problem("环境变量包含不支持的搜索来源", 503)
            defaults["enabled_search_sources"] = enabled
        with self.connect() as db:
            settings = {**defaults, **{r["key"]: json.loads(r["value"])
                                       for r in db.execute("SELECT * FROM settings")
                                       if r["key"] in DEFAULTS or r["key"] == "115_account_verified"}}
        # Retired settings remain in the database for recovery, but never enter current API responses.
        enabled = settings["enabled_search_sources"]
        if isinstance(enabled, list):
            settings["enabled_search_sources"] = list(dict.fromkeys(key for key in enabled
                                                                    if key in SEARCH_SOURCE_IDS))
        return settings

    def configure(self, values):
        with self.connect() as db:
            for k, v in values.items():
                db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, json.dumps(v, ensure_ascii=False)))

    def cached_metadata(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM metadata_cache WHERE key=?", (key,)).fetchone()
        return {**dict(row), "data": json.loads(row["data"])} if row else None

    def cache_metadata(self, key, data, ttl):
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO metadata_cache VALUES (?,?,?,?)",
                       (key, json.dumps(data, ensure_ascii=False), now, now+ttl))
            db.execute("DELETE FROM metadata_cache WHERE key IN (SELECT key FROM metadata_cache "
                       "ORDER BY updated DESC LIMIT -1 OFFSET 1000)")
        return now

    def channels(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM channels ORDER BY title")]

    def channel(self, channel_id, **updates):
        allowed = {"enabled", "cursor", "highwater", "count", "complete", "status", "error", "wait_until", "title",
                   "last_synced", "refresh_before", "refresh_highwater", "refresh_at", "failures"}
        with self.connect() as db:
            if updates:
                if not set(updates) <= allowed:
                    raise ValueError("invalid channel fields")
                db.execute("UPDATE channels SET " + ",".join(k + "=?" for k in updates) + " WHERE id=?",
                           (*updates.values(), channel_id))
            row = db.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
            if not row:
                raise Problem("频道不存在", 404)
            return dict(row)

    def put_message(self, channel_id, message_id, body, resources, published, revision=0):
        with self.connect() as db:
            self._put_message(db, channel_id, message_id, body, resources, published, revision)

    def commit_web_page(self, channel_id, messages, updates):
        """Commit a page and its checkpoint together; a paused/removed channel cannot advance."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT enabled,mode FROM channels WHERE id=?", (channel_id,)).fetchone()
            if not row or not row["enabled"] or row["mode"] != "public_web":
                return
            for message in messages:
                self._put_message(db, channel_id, **message)
            allowed = {"cursor", "highwater", "complete", "status", "error", "wait_until", "last_synced",
                       "refresh_before", "refresh_highwater", "refresh_at", "failures", "title"}
            if not set(updates) <= allowed:
                raise ValueError("invalid checkpoint fields")
            db.execute("UPDATE channels SET " + ",".join(k + "=?" for k in updates) + " WHERE id=?",
                       (*updates.values(), channel_id))

    def _put_message(self, db, channel_id, message_id, body, resources, published, revision=0):
        if not db.execute("SELECT 1 FROM channels WHERE id=?", (channel_id,)).fetchone():
            return
        current = db.execute("SELECT revision FROM messages WHERE channel_id=? AND message_id=?", (channel_id, message_id)).fetchone()
        if body is not None:
            if db.execute("SELECT 1 FROM deleted_messages WHERE channel_id=? AND message_id=?", (channel_id, message_id)).fetchone():
                return
            if current and current[0] > revision:
                return
        db.execute("DELETE FROM message_fts WHERE channel_id=? AND message_id=?", (channel_id, message_id))
        if body is None:
            db.execute("DELETE FROM messages WHERE channel_id=? AND message_id=?", (channel_id, message_id))
            db.execute("INSERT OR IGNORE INTO deleted_messages VALUES (?,?)", (channel_id, message_id))
        else:
            terms = tokenize(body)
            db.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?)",
                       (channel_id, message_id, body, terms, published, json.dumps(resources, ensure_ascii=False), revision))
            db.execute("INSERT INTO message_fts VALUES (?,?,?)", (terms, channel_id, message_id))
        delta = (-1 if current else 0) if body is None else (0 if current else 1)
        db.execute("UPDATE channels SET count=max(0,count+?) WHERE id=?", (delta, channel_id))

    def search_messages(self, query, limit=200):
        terms = list(dict.fromkeys(tokenize(query).split()))
        if not terms:
            return []
        expression = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)
        with self.connect() as db:
            rows = db.execute("""SELECT m.* FROM message_fts f JOIN messages m
              ON m.channel_id=f.channel_id AND m.message_id=f.message_id
              JOIN channels c ON c.id=m.channel_id
              WHERE message_fts MATCH ? AND c.enabled=1 ORDER BY m.published DESC LIMIT ?""", (expression, limit)).fetchall()
            return [dict(r) for r in rows]

    def recent_messages(self, limit=3000):
        """Newest posts of enabled channels, for the homepage feed."""
        with self.connect() as db:
            rows = db.execute("""SELECT m.channel_id,m.message_id,m.body,m.published,m.resources FROM messages m
              JOIN channels c ON c.id=m.channel_id WHERE c.enabled=1 AND m.published IS NOT NULL
              ORDER BY m.published DESC LIMIT ?""", (limit,)).fetchall()
        return [{**dict(r), "resources": json.loads(r["resources"])} for r in rows]

    def record_releases(self, rows, since):
        """Keep torrents seen by the homepage feed (one row per info hash) and drop those older than ``since``."""
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO home_releases VALUES (?,?,?,?)",
                           [(r["hash"], r["item"], r["published"], json.dumps(r, ensure_ascii=False)) for r in rows
                            if r["published"] >= since])
            db.execute("DELETE FROM home_releases WHERE published<?", (since,))

    def recent_releases(self, since):
        with self.connect() as db:
            rows = db.execute("SELECT data FROM home_releases WHERE published>=?", (since,)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def task(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise Problem("任务不存在", 404)
        return {**dict(row), "payload": json.loads(row["payload"])}

    def tasks(self, active=False):
        with self.connect() as db:
            rows = db.execute("SELECT id FROM tasks " + ("WHERE state NOT IN ('completed','canceled','needs_attention','naming_review') " if active else "")
                              + "ORDER BY created DESC LIMIT 500").fetchall()
        return [self.task(r["id"]) for r in rows]

    def create_task(self, key, target, payload):
        import uuid
        task_id, now = str(uuid.uuid4()), time.time()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
                       (task_id, key, target, json.dumps(payload, ensure_ascii=False), "queued", 0, None, now, now))
            row = db.execute("SELECT id FROM tasks WHERE resource_key=? AND target=?", (key, target)).fetchone()
        return self.task(row["id"])

    def update_task(self, task_id, state=None, error=None, progress=None, **payload):
        task = self.task(task_id)
        next_payload = {**task["payload"], **payload}
        with self.connect() as db:
            db.execute("UPDATE tasks SET state=?,error=?,progress=?,payload=?,updated=? WHERE id=?",
                       (state or task["state"], error, task["progress"] if progress is None else progress,
                        json.dumps(next_payload, ensure_ascii=False), time.time(), task_id))
            if state and state != task["state"]:
                db.execute("INSERT INTO events(task_id,state,message,created) VALUES (?,?,?,?)",
                           (task_id, state, error, time.time()))
        return self.task(task_id)

    def subscriptions(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM subscriptions ORDER BY created DESC").fetchall()
        return [{**dict(r), "rules": json.loads(r["rules"])} for r in rows]

    def create_subscription(self, values):
        import uuid
        subscription_id = str(uuid.uuid4())
        with self.connect() as db:
            try:
                db.execute("INSERT INTO subscriptions(id,bangumi_id,subgroup_id,subgroup_name,title,poster,media_type,"
                           "media_id,season_number,target,rules,backfill,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (subscription_id, values["bangumi_id"], values["subgroup_id"], values["subgroup_name"],
                            values["title"], values["poster"], values["media_type"], values["media_id"],
                            values["season_number"], values["target"], json.dumps(values["rules"], ensure_ascii=False),
                            int(values["backfill"]), time.time()))
            except sqlite3.IntegrityError:
                raise Problem("已订阅该番剧的这个字幕组", 409) from None
        return self.subscription(subscription_id)

    def subscription(self, subscription_id, **updates):
        allowed = {"enabled", "rules", "target", "checked", "next_check", "error", "failures", "initialized"}
        if "rules" in updates:
            updates["rules"] = json.dumps(updates["rules"], ensure_ascii=False)
        with self.connect() as db:
            if updates:
                if not set(updates) <= allowed:
                    raise ValueError("invalid subscription fields")
                db.execute("UPDATE subscriptions SET " + ",".join(k + "=?" for k in updates) + " WHERE id=?",
                           (*updates.values(), subscription_id))
            row = db.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,)).fetchone()
        if not row:
            raise Problem("订阅不存在", 404)
        return {**dict(row), "rules": json.loads(row["rules"])}

    def delete_subscription(self, subscription_id):
        """Tasks and library files created by the subscription are kept."""
        with self.connect() as db:
            db.execute("DELETE FROM subscription_episodes WHERE subscription_id=?", (subscription_id,))
            if not db.execute("DELETE FROM subscriptions WHERE id=?", (subscription_id,)).rowcount:
                raise Problem("订阅不存在", 404)

    def claim_episode(self, subscription_id, episode, key, title, task_id):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO subscription_episodes VALUES (?,?,?,?,?,?)",
                       (subscription_id, episode, key, title, task_id, time.time()))

    def subscription_episodes(self, subscription_id):
        with self.connect() as db:
            rows = db.execute("""SELECT e.*,t.state,t.progress,t.error FROM subscription_episodes e
              LEFT JOIN tasks t ON t.id=e.task_id WHERE e.subscription_id=? ORDER BY e.episode""",
                              (subscription_id,)).fetchall()
        return [dict(r) for r in rows]

    def new_session(self):
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?)", (hashlib.sha256(token.encode()).hexdigest(), time.time()+86400*7))
        return token

    def session_valid(self, token):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM sessions WHERE digest=? AND expires>?",
                                   (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone())

    def revoke_sessions(self):
        with self.connect() as db:
            db.execute("DELETE FROM sessions")


async def blocking(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)
