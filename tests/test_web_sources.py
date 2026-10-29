import asyncio
import json
import sqlite3
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from medialink.app import create_app
from medialink.core import DEFAULTS, Problem, Store, resource_key
from medialink.sources import Telegram, extract_resources, merge_results
from medialink.web_sources import Knaben, LimeTorrents, TelegramWeb, WebRateLimit, public_username
from test_medialink import MAGNET, MemoryVault, authenticate


@pytest.fixture
def web_store(tmp_path):
    result = Store(tmp_path / "medialink.db")
    with result.connect() as db:
        db.execute("INSERT INTO channels(id,peer,title,mode) VALUES ('web:sample','sample','影视资源','public_web')")
    return result


def web_html(ids, before=None, title="影视资源", bodies=None):
    messages = []
    for mid in ids:
        body = bodies.get(mid, "星际穿越 1080p") if bodies else "星际穿越 1080p"
        messages.append(f'''<div class="tgme_widget_message" data-post="sample/{mid}">
          <div class="tgme_widget_message_text">{body}<br/>提取码：aB12
          <a href="{MAGNET}&amp;dn=movie">磁力</a><a href="https://115.com/s/example">网盘</a></div>
          <time datetime="2026-10-01T12:00:00+00:00"></time></div>''')
    more = f'<a class="tme_messages_more" href="/s/sample?before={before}">Older</a>' if before else ""
    return '<div class="tgme_channel_info"><div class="tgme_channel_info_header_title">'+title+'</div></div>'+"".join(messages)+more


def page(ids, before=None, bodies=None):
    return TelegramWeb.parse_page(web_html(ids, before, bodies=bodies), "sample")


def knaben_hit(**changes):
    return {"title": "Open Film 1080p", "magnetUrl": MAGNET, "bytes": 1024**3, "date": "2026-10-01T00:00:00Z",
            "seeders": 0, "tracker": "The Pirate Bay", "category": "Movies / HD", "categoryId": [3000000,3001000],
            "details": "https://example.org/view/1", **changes}


def test_knaben_validation_categories_metadata_and_dedup():
    data = {"hits": [knaben_hit(), knaben_hit(tracker="Nyaa"),
                     knaben_hit(magnetUrl="magnet:?xt=bad"), knaben_hit(tracker="1337x"),
                     knaben_hit(tracker="Other", trackerId="1337x"),
                     knaben_hit(category="Games", categoryId=[4001000]),
                     knaben_hit(title="Different version", magnetUrl="magnet:?xt=urn:btih:"+"b"*40,
                                bytes=None, date="broken", seeders=None, details="javascript:alert(1)")]}
    results = Knaben.parse(data)
    assert len(results) == 2
    assert len(results[0]["sources"]) == 2
    assert results[0]["seeders"] == "0" and results[0]["size"] == "1.0 GiB"
    assert results[0]["resolution"] == "1080P" and results[0]["published"]
    assert results[1]["size"] is None and results[1]["published"] is None
    assert results[1]["sources"][0]["url"] == "https://knaben.org"
    with pytest.raises(Problem):
        Knaben.parse({"hits": "unexpected"})


async def test_knaben_request_cache_and_timeout(monkeypatch):
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"hits": [knaben_hit()]})
    source = Knaben(lambda: DEFAULTS)
    source.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert len(await source.search("公开影片")) == 1
    assert len(await source.search("公开影片")) == 1 and len(requests) == 1
    assert requests[0].url.params["q"] == "公开影片"
    assert requests[0].url.params["c"] == "2000000,3000000"
    assert requests[0].url.params["s"] == "50"
    source.fetch = AsyncMock(side_effect=httpx.ReadTimeout("private-response-secret"))
    with pytest.raises(Problem, match="超时") as error:
        await source.search("new-query")
    assert "private-response-secret" not in str(error.value)


def lime_row(mid, category="Movies", extra=""):
    return f'''<tr bgcolor="white"><td><div class="tt-name"><a href="/leet2/?ad=1">ad</a>
      <a href="/Open-Film-1080p-torrent-{mid}.html">Open Film 1080p</a>{extra}</div></td>
      <td>today - in {category}</td><td>1.2 GiB</td><td class="tdseed">12</td></tr>'''


def test_lime_real_details_ignore_ads_and_non_media():
    body = '<table class="table2">'+lime_row(1)+lime_row(2,"Applications")+lime_row(1)+'</table>'
    items = LimeTorrents.parse_search(body,"https://example.org")
    assert len(items) == 1 and items[0]["detail"].endswith("-torrent-1.html")
    assert items[0]["published"] is None
    results = LimeTorrents.parse_detail(f'<a href="{MAGNET}">Magnet</a><a href="magnet:?xt=bad">bad</a>',items[0])
    assert len(results) == 1 and results[0]["seeders"] == "12"
    assert results[0]["sources"][0]["provider"] == "limetorrents"
    assert len(merge_results(results + Knaben.parse({"hits":[knaben_hit()]}))) == 1
    with pytest.raises(Problem, match="要求人机验证"):
        LimeTorrents.parse_search("<h1>Verify you are human</h1>","https://example.org")


async def test_lime_limits_concurrency_and_partial_detail_failure():
    active = peak = calls = 0
    async def handler(request):
        nonlocal active, peak, calls
        calls += 1
        if request.url.path.startswith("/search/"):
            return httpx.Response(200,text='<table class="table2">'+"".join(lime_row(i) for i in range(30))+'</table>')
        assert "/leet2/" not in request.url.path
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(.001)
        active -= 1
        mid = int(request.url.path.rsplit("-",1)[1].split(".")[0])
        if mid == 0:
            return httpx.Response(503)
        return httpx.Response(200,text=f'<a href="magnet:?xt=urn:btih:{mid:040x}">magnet</a>')
    source = LimeTorrents(lambda: DEFAULTS)
    source.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    results = await source.search("公开影片")
    assert len(results) == 19 and calls == 21 and peak == 3


@pytest.mark.parametrize("peer", ["@Sample","https://t.me/sample","t.me/sample","https://t.me/s/sample/","https://t.me/115movie"])
def test_public_channel_names(peer):
    assert public_username(peer) in {"sample","115movie"}


@pytest.mark.parametrize("peer", ["https://evil.example/sample","https://t.me/+secret","https://t.me/c/123/4",
                                  "https://t.me/sample/12","-10012345","12345","https://t.me/joinchat/secret"])
def test_public_channel_rejects_private_or_message_links(peer):
    with pytest.raises(Problem):
        public_username(peer)
    with pytest.raises(Problem):
        public_username('https://[bad')


def test_public_page_hidden_links_codes_and_unsupported_posts():
    body = web_html([10],before=10)+'''<div class="tgme_widget_message" data-post="sample/11">
      <div class="tgme_widget_message_error">VIEW IN TELEGRAM</div></div>'''
    result = TelegramWeb.parse_page(body,"sample",revision=123)
    assert result["ids"] == [10,11] and len(result["messages"]) == 1
    message = result["messages"][0]
    assert message["body"].startswith("星际穿越 1080p\n")
    assert message["revision"] == 123 and message["published"]
    assert len(message["resources"]) == 2
    assert message["resources"][1]["password"] == "aB12"
    assert message["resources"][0]["sources"][0]["url"] == "https://t.me/sample/10"
    with pytest.raises(Problem):
        TelegramWeb.parse_page("<h1>Channel private</h1>","sample")
    with pytest.raises(Problem,match="翻页"):
        TelegramWeb.parse_page(web_html([10],before=99),"sample")


async def test_public_history_restart_and_message_edits_without_deletion(web_store):
    web = TelegramWeb(web_store)
    web.fetch_page = AsyncMock(side_effect=[page([40,41],40),page(range(20,40),20),page(range(1,20))])
    await web.sync_channel(web_store.channel("web:sample"))
    assert web_store.channel("web:sample")["cursor"] == 40
    reopened = Store(web_store.path)
    web.store = reopened
    await web.sync_channel(reopened.channel("web:sample"))
    assert reopened.channel("web:sample")["cursor"] == 20
    await web.sync_channel(reopened.channel("web:sample"))
    channel = reopened.channel("web:sample")
    assert channel["complete"] == 1 and channel["count"] == 41
    assert "可见历史" in channel["status"] and len(reopened.search_messages("星际穿越")) == 41
    web.fetch_page = AsyncMock(return_value=page([41],bodies={41:"流浪地球 2160p"}))
    reopened.channel("web:sample",refresh_at=0)
    await web.sync_channel(reopened.channel("web:sample"))
    assert len(reopened.search_messages("流浪地球")) == 1
    assert len(reopened.search_messages("星际穿越")) == 40
    assert reopened.channel("web:sample")["count"] == 41


async def test_public_incremental_new_messages_more_than_one_page(web_store):
    web_store.channel("web:sample",highwater=41,cursor=1,complete=1)
    web = TelegramWeb(web_store)
    web.fetch_page = AsyncMock(side_effect=[page([64,65],64),page(range(44,64),44),page(range(40,44),40)])
    await web.sync_channel(web_store.channel("web:sample"))
    assert web_store.channel("web:sample")["highwater"] == 41
    assert web_store.channel("web:sample")["refresh_before"] == 64
    web.store = Store(web_store.path)
    await web.sync_channel(web.store.channel("web:sample"))
    assert web.store.channel("web:sample")["refresh_before"] == 44
    await web.sync_channel(web.store.channel("web:sample"))
    channel = web.store.channel("web:sample")
    assert channel["highwater"] == 65 and channel["refresh_before"] == 0
    assert channel["cursor"] == 1 and channel["refresh_at"] > time.time()+290
    assert channel["count"] == 26


async def test_public_page_and_cursor_are_atomic_and_pause_during_request(web_store,monkeypatch):
    web = TelegramWeb(web_store)
    web.fetch_page = AsyncMock(return_value=page([10,11],10))
    original = web_store._put_message
    calls = 0
    def crash(*args,**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated crash")
        return original(*args,**kwargs)
    monkeypatch.setattr(web_store,"_put_message",crash)
    with pytest.raises(RuntimeError):
        await web.sync_channel(web_store.channel("web:sample"))
    assert web_store.channel("web:sample")["count"] == 0
    assert web_store.channel("web:sample")["highwater"] == 0
    assert not web_store.search_messages("星际穿越")
    monkeypatch.setattr(web_store,"_put_message",original)
    async def paused(*args):
        web_store.channel("web:sample",enabled=0)
        return page([10,11],10)
    web.fetch_page = paused
    await web.sync_channel(web_store.channel("web:sample"))
    assert web_store.channel("web:sample")["count"] == 0
    web_store.channel("web:sample",enabled=1)
    web.fetch_page = AsyncMock(return_value=page([10,11],10))
    await web.sync_channel(web_store.channel("web:sample"))
    assert web_store.channel("web:sample")["count"] == 2


async def test_public_rate_limit_backoff_and_request_interval(web_store,monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429,headers={"Retry-After":"120"})
    web = TelegramWeb(web_store)
    web.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    web.last_request = time.monotonic()
    sleep = AsyncMock()
    monkeypatch.setattr("medialink.web_sources.asyncio.sleep",sleep)
    await web.run_once()
    channel = web_store.channel("web:sample")
    assert channel["wait_until"] > time.time()+115 and channel["failures"] == 1
    assert channel["status"] == "公共网页限流等待"
    assert sleep.call_args.args[0] > 2.5
    await web.run_once()
    assert len(calls) == 1
    reopened = TelegramWeb(Store(web_store.path))
    with pytest.raises(WebRateLimit):
        await reopened.fetch_page("another")
    web_store.channel("web:sample",wait_until=0)
    web.fetch_page = AsyncMock(side_effect=Problem("inaccessible",422))
    await web.run_once()
    assert web_store.channel("web:sample")["wait_until"] > time.time()+55
    assert web_store.channel("web:sample")["highwater"] == 0


async def test_api_worker_does_not_login_for_web_channels(web_store,monkeypatch):
    tg = Telegram(web_store,MemoryVault())
    tg.authorized = AsyncMock(return_value=True)
    async def stop(seconds):
        tg.stopped = True
    monkeypatch.setattr("medialink.sources.asyncio.sleep",stop)
    await tg.run()
    tg.authorized.assert_not_called()


@pytest.mark.parametrize("failure,message", [(Problem("Nyaa unavailable",502),"Nyaa unavailable"),
                                           (TimeoutError(),"该来源搜索超时")])
def test_new_sources_api_channel_modes_and_search_task_submission(web_store,failure,message):
    vault = MemoryVault()
    app = create_app(web_store,vault,start_workers=False)
    resource = Knaben.parse({"hits":[knaben_hit()]})
    app.state.knaben.search = AsyncMock(return_value=resource)
    app.state.limetorrents.search = AsyncMock(return_value=resource)
    app.state.nyaa.search = AsyncMock(side_effect=failure)
    app.state.telegram.search = AsyncMock(return_value=[])
    app.state.telegram_web.fetch_page = AsyncMock(return_value=page([10]))
    app.state.openlist.get = AsyncMock(return_value={"is_dir":True})
    with TestClient(app) as client:
        assert client.get('/api/v1/sources').status_code == 401
        authenticate(client)
        assert [s['id'] for s in client.get('/api/v1/sources').json()] == ['knaben','limetorrents','nyaa','telegram']
        channel = client.post('/api/v1/channels',json={'peer':'@Sample'}).json()
        assert channel['mode'] == 'public_web'
        assert client.post('/api/v1/channels',json={'peer':'sample','mode':'wrong'}).status_code == 422
        assert client.post('/api/v1/channels',json={'peer':'sample','mode':[]}).status_code == 422
        assert client.patch('/api/v1/channels/web:sample',json={'enabled':False}).json()['enabled'] == 0
        assert client.patch('/api/v1/channels/web:sample',json={'enabled':True}).json()['refresh_at'] == 0
        search = client.post('/api/v1/search',json={'query':'Open Film'}).json()
        events = client.get('/api/v1/search/'+search['id']+'/events').text
        assert message in events and 'event: done' in events
        done = json.loads(events.split('event: done\ndata: ')[1].split('\n')[0])['results']
        assert len(done) == 1
        selected = done[0]
        payload = {'link':selected['link'],'title':selected['title'],'target':'/115/infuse','sources':selected['sources']}
        task = client.post('/api/v1/tasks',json=payload).json()
        assert task['sources'] == selected['sources']
        assert task['id'] == client.post('/api/v1/tasks',json=payload).json()['id']
        stored = web_store.task(task['id'])
        assert stored['resource_key'] == resource_key(selected['link'])
        assert json.loads(vault.unseal(stored['payload']['link_enc']))['link'] == MAGNET
        assert client.post('/api/v1/search',json={'query':'x','sources':['unknown']}).status_code == 422


def test_old_database_migration_and_sqlite_backup(tmp_path):
    tmp_path = tmp_path / 'Application Support' / 'MediaLink'
    tmp_path.mkdir(parents=True)
    path = tmp_path/'medialink.db'
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("CREATE TABLE channels(id TEXT PRIMARY KEY,peer TEXT NOT NULL,title TEXT NOT NULL,enabled INTEGER DEFAULT 1,"
                   "cursor INTEGER DEFAULT 0,highwater INTEGER DEFAULT 0,count INTEGER DEFAULT 0,complete INTEGER DEFAULT 0,"
                   "status TEXT DEFAULT '等待同步',error TEXT,wait_until REAL DEFAULT 0)")
        db.execute("INSERT INTO channels(id,peer,title,cursor,highwater,count) VALUES ('-1001','-1001','旧频道',80,100,1)")
    import os
    result = subprocess.run(['/bin/zsh',str(Path(__file__).parents[1]/'scripts/backup.sh')],
                            env={**os.environ,'MEDIALINK_DATA':str(tmp_path)},capture_output=True,text=True)
    assert result.returncode == 0
    backup = next((tmp_path/'backups').glob('*.db'))
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as db:
        assert db.execute('SELECT cursor FROM channels').fetchone()[0] == 80
        assert 'mode' not in {r[1] for r in db.execute('PRAGMA table_info(channels)')}
    migrated = Store(path)
    channel = migrated.channel('-1001')
    assert channel['mode'] == 'api' and channel['cursor'] == 80 and channel['highwater'] == 100
    assert channel['count'] == 1 and channel['last_synced'] == 0
    migrated.put_message('-1001',80,'星际穿越',extract_resources('电影',[MAGNET]),None,10)
    assert Store(path).search_messages('星际穿越')


async def test_public_ignored_pagination_is_not_committed(web_store):
    web_store.channel('web:sample',cursor=10,highwater=11,refresh_at=time.time()+300)
    web = TelegramWeb(web_store)
    web.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(200,text=web_html([10,11],10))))
    await web.run_once()
    channel = web_store.channel('web:sample')
    assert channel['cursor'] == 10 and channel['count'] == 0
    assert channel['failures'] == 1 and '未向前翻页' in channel['error']


async def test_public_inaccessible_page_and_lime_unreadable_details(web_store):
    web = TelegramWeb(web_store)
    web.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(200,text='<h1>Private channel</h1>')))
    await web.run_once()
    assert web_store.channel('web:sample')['cursor'] == 0
    assert '公开网页预览' in web_store.channel('web:sample')['error']
    source = LimeTorrents(lambda:DEFAULTS)
    def handler(request):
        return httpx.Response(200,text='<table class="table2">'+lime_row(1)+'</table>'
                              if request.url.path.startswith('/search/') else '<div class="torrentinfo">Torrent info</div>')
    source.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(Problem,match='没有有效磁力'):
        await source.search('film')
