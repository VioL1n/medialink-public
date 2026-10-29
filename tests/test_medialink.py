import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from medialink.app import create_app
from medialink.clients import RemoteFailure
from medialink.core import Problem, Store, resource_key, target_path
from medialink.sources import Nyaa, Telegram, extract_resources, merge_results
from medialink.worker import Worker
from telethon import errors

MAGNET = "magnet:?xt=urn:btih:" + "a"*40


class MemoryVault:
    def __init__(self):
        self.values = {}
        self.fernet = Fernet(Fernet.generate_key())

    def get(self, name, service=None):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)

    def seal(self, value):
        return self.fernet.encrypt(value.encode()).decode()

    def unseal(self, value):
        return self.fernet.decrypt(value.encode()).decode()


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "state.db")
    result.configure({"strm_local": str(tmp_path / "strm")})
    with result.connect() as db:
        db.execute("INSERT INTO channels(id,peer,title) VALUES ('-1001','-1001','影视资源')")
    return result


def test_resource_identity_and_directory_boundary():
    encoded = base64.b32encode(bytes.fromhex("a"*40)).decode()
    assert resource_key("magnet:?xt=urn:btih:"+encoded) == resource_key(MAGNET)
    assert resource_key("https://115.com/s/abc?password=1234") == "share:abc"
    assert target_path("/115/infuse/电影", "/115/infuse") == "/115/infuse/电影"
    for link in ["magnet:?xt=bad", "https://115.com.evil.example/s/abc", "https://115.com/s/abc/extra"]:
        with pytest.raises(Problem):
            resource_key(link)
    for path in ["/115/infuse2", "/115/infuse/../other", "relative", "/115/infuse/\x00bad"]:
        with pytest.raises(Problem):
            target_path(path, "/115/infuse")


def test_hidden_links_codes_and_versions():
    text = "星际穿越 2014 2160p\n提取码：aB12"
    found = extract_resources(text, [MAGNET, "https://115.com/s/collection"], {"name": "频道", "url": "https://t.me/x/1"})
    assert len(found) == 2
    assert found[0]["resolution"] == "2160P"
    assert found[1]["password"] == "aB12"
    second = {**found[0], "sources": [{"name": "Nyaa", "url": "https://nyaa.si/view/1"}]}
    assert len(merge_results([found[0], second])) == 1
    assert len(merge_results([found[0], second])[0]["sources"]) == 2
    assert len(merge_results([found[0], {**found[0], "key": "magnet:"+"b"*40}])) == 2


def test_chinese_index_overlap_edits_and_tombstones(store):
    resource = extract_resources("星际穿越 1080p", [MAGNET])
    store.put_message("-1001", 12, "星际穿越 高清电影", resource, "2026-10-01", 10)
    store.put_message("-1001", 12, "星际穿越 高清电影", resource, "2026-10-01", 10)
    assert len(store.search_messages("星际穿越")) == 1
    assert store.channel("-1001")["count"] == 1
    store.put_message("-1001", 12, "流浪地球 高清电影", resource, "2026-10-01", 20)
    store.put_message("-1001", 12, "旧版本 星际穿越", resource, "2026-10-01", 10)
    assert not store.search_messages("星际穿越")
    assert len(store.search_messages("流浪地球")) == 1
    store.channel("-1001", enabled=0)
    assert not store.search_messages("流浪地球")
    store.channel("-1001", enabled=1)
    store.put_message("-1001", 12, None, [], None)
    store.put_message("-1001", 12, "流浪地球", resource, "2026-10-01", 20)
    assert not store.search_messages("流浪地球")


def test_task_uniqueness_persistence_and_encryption(store):
    vault = MemoryVault()
    task = store.create_task(resource_key(MAGNET), "/115/infuse", {"kind":"magnet", "link_enc":vault.seal(MAGNET)})
    assert store.create_task(resource_key(MAGNET), "/115/infuse", {"kind":"magnet"})["id"] == task["id"]
    store.update_task(task["id"], "downloading", external_id="remote-123")
    reopened = Store(store.path)
    assert reopened.task(task["id"])["state"] == "downloading"
    assert reopened.task(task["id"])["payload"]["external_id"] == "remote-123"
    assert MAGNET not in store.path.read_bytes().decode("utf-8", "ignore")


def test_nyaa_parser_metadata():
    body = f'''<table class="torrent-list"><tbody><tr><td><a href="/?c=1_2">Anime</a></td>
      <td><a class="comments" href="/view/1#comments">3</a><a href="/view/1">Open Movie 1080p</a></td>
      <td><a href="{MAGNET}">magnet</a></td><td>1.2 GiB</td><td data-timestamp="1700000000">Date</td>
      <td>50</td><td>4</td><td>100</td></tr></tbody></table>'''
    found = Nyaa.parse(body, "https://nyaa.si")
    assert len(found) == 1
    assert found[0]["size"] == "1.2 GiB" and found[0]["seeders"] == "50"
    assert found[0]["title"] == "Open Movie 1080p" and found[0]["published"]


class FakeOpenList:
    def __init__(self):
        self.fs = {"/115/infuse": []}
        self.remote = []
        self.offline_calls = self.move_calls = self.scan_calls = 0
        self.scan_busy = False
        self.timeout_submit = False
        self.timeout_move = False

    async def mkdir(self, path):
        self.fs.setdefault(path, [])

    async def get(self, path):
        return {"name": "directory", "is_dir": True}

    async def request(self, method, endpoint, *args, **kwargs):
        return ["115 Open"]

    async def entries(self, path, refresh=True):
        return self.fs.get(path, [])

    async def remote_tasks(self):
        return self.remote

    async def native_status(self, infohash, staging):
        return None

    async def offline(self, link, path):
        self.offline_calls += 1
        self.remote.append({"id": "mine", "name": path, "state": 2, "bucket": "done", "progress":100})
        self.fs[path] = [{"name":"open-movie.mkv","size":1234,"is_dir":False}]
        if self.timeout_submit:
            raise RemoteFailure("timeout", uncertain=True)
        return "mine"

    async def move(self, src, dst, names):
        self.move_calls += 1
        self.fs.setdefault(dst, []).extend(self.fs[src])
        self.fs[src] = []
        if self.timeout_move:
            raise RemoteFailure("timeout", uncertain=True)

    async def scan_status(self):
        return {"is_done": not self.scan_busy}

    async def scan(self, path):
        self.scan_calls += 1

    async def cancel(self, task_id):
        return None


class FakeP115:
    def __init__(self, openlist):
        self.openlist = openlist
        self.calls = 0
        self.timeout = False

    async def share_entries(self, link, password, folder="0"):
        return [{"id":"123","name":"open-movie.mkv","is_dir":False,"size":1234}]

    async def folder_id(self, staging):
        self.staging = staging
        return "folder"

    async def share_tree(self, link, password, selected):
        return [{"path":entry["name"],"size":entry["size"]} for entry in selected]

    async def receive(self, *args):
        self.calls += 1
        self.openlist.fs[self.staging] = [{"name":"open-movie.mkv","size":1234,"is_dir":False}]
        if self.timeout:
            raise RemoteFailure("timeout", uncertain=True)


def new_worker(store, kind="magnet"):
    vault, openlist = MemoryVault(), FakeOpenList()
    p115 = FakeP115(openlist)
    worker = Worker(store, vault, openlist, p115)
    link = MAGNET if kind == "magnet" else "https://115.com/s/testshare"
    task = store.create_task(resource_key(link), "/115/infuse", {"kind":kind,"title":"Open Movie",
                             "link_enc":vault.seal(json.dumps({"link":link,"password":"1234"}))})
    return worker, task, openlist, p115


async def drive(worker, task, terminal="completed", iterations=30):
    for _ in range(iterations):
        latest = worker.store.task(task["id"])
        if latest["state"] == terminal:
            return latest
        await worker.step(latest)
    return worker.store.task(task["id"])


@pytest.mark.parametrize("kind", ["magnet", "share"])
async def test_full_ingest_preserves_files_and_notifies_only_after_strm(store, monkeypatch, kind):
    worker, task, ol, p115 = new_worker(store, kind)
    notify = AsyncMock()
    monkeypatch.setattr("medialink.worker.refresh_emby", notify)
    result = await drive(worker, task, terminal="scanning")
    await worker.step(result)
    assert not notify.called
    output = Path(store.settings()["strm_local"])/"infuse/open-movie.strm"
    output.parent.mkdir(parents=True)
    output.write_text("http://localhost:5244/d/115/infuse/open-movie.mkv")
    result = await drive(worker, task)
    assert result["state"] == "completed"
    assert ol.fs["/115/infuse"][0]["name"] == "open-movie.mkv"
    assert ol.offline_calls == (1 if kind == "magnet" else 0)
    assert p115.calls == (1 if kind == "share" else 0)
    notify.assert_awaited_once()


@pytest.mark.parametrize("kind", ["magnet", "share"])
async def test_timeout_submission_and_restart_never_duplicates(store, kind):
    worker, task, ol, p115 = new_worker(store, kind)
    ol.timeout_submit = p115.timeout = True
    await drive(worker, task, terminal="reconciling")
    # The first reconciling state follows an actual remote write, persisted before it.
    assert ol.offline_calls+p115.calls == 1
    worker.store = Store(store.path)
    result = await drive(worker, task, terminal="organizing")
    assert result["state"] == "organizing"
    assert ol.offline_calls+p115.calls == 1


async def test_unrelated_remote_tasks_are_not_assigned(store):
    worker, task, ol, _ = new_worker(store)
    await drive(worker, task, terminal="ready_submit")
    worker.store.update_task(task["id"], "reconciling", submitted_at=time.time(), baseline=[])
    ol.remote=[{"id":"other","name":MAGNET+" to /115/somewhere-else","state":2}]
    await worker.step(store.task(task["id"]))
    assert store.task(task["id"])["state"] == "reconciling"


async def test_name_conflict_keeps_staging_and_allows_new_target(store):
    worker, task, ol, _ = new_worker(store)
    await drive(worker, task, terminal="organizing")
    ol.fs["/115/infuse"]=[{"name":"open-movie.mkv","size":99,"is_dir":False}]
    with pytest.raises(Problem):
        await worker.step(store.task(task["id"]))
    assert ol.move_calls == 0
    latest = store.task(task["id"])
    assert ol.fs[latest["payload"]["staging"]]
    store.update_task(task["id"], "needs_attention", resume_state="organizing")
    changed = await worker.retry(task["id"], "/115/infuse/new")
    assert changed["target"] == "/115/infuse/new"


async def test_move_timeout_reconciles_instead_of_moving_again(store):
    worker, task, ol, _ = new_worker(store)
    ol.timeout_move = True
    result = await drive(worker, task, terminal="scan_queued")
    assert result["state"] == "scan_queued" and ol.move_calls == 1


async def test_native_115_completion_recovers_openlist_duplicate_error(store):
    worker, task, ol, _ = new_worker(store)
    await drive(worker, task, terminal='downloading')
    ol.remote[0]['error']='任务已存在，请勿输入重复的链接地址'
    ol.native_status=AsyncMock(return_value={"status":2,"progress":100,"size":1234,"folder_id":"mine"})
    await worker.step(store.task(task['id']))
    assert store.task(task['id'])['state']=='transferring'
    ol.fs[store.task(task['id'])['payload']['staging']][0]['size']=100
    await worker.step(store.task(task['id']))
    assert store.task(task['id'])['state']=='transferring'
    ol.fs[store.task(task['id'])['payload']['staging']][0]['size']=1234
    await worker.step(store.task(task['id']))
    assert store.task(task['id'])['state']=='organizing'


async def test_scan_busy_waits_and_notification_retry_skips_download(store, monkeypatch):
    worker, task, ol, _ = new_worker(store)
    await drive(worker, task, terminal="scan_queued")
    ol.scan_busy = True
    await worker.step(store.task(task["id"]))
    assert ol.scan_calls == 0
    ol.scan_busy = False
    output = Path(store.settings()["strm_local"])/"infuse/open-movie.strm"
    output.parent.mkdir(parents=True)
    output.write_text("http://localhost/video")
    await drive(worker, task, terminal="notifying")
    notify = AsyncMock(side_effect=RemoteFailure("Emby unavailable"))
    monkeypatch.setattr("medialink.worker.refresh_emby", notify)
    with pytest.raises(RemoteFailure):
        await worker.step(store.task(task["id"]))
    assert store.task(task["id"])["state"] == "notifying"
    monkeypatch.setattr("medialink.worker.refresh_emby", AsyncMock())
    worker.store = Store(store.path)
    assert (await drive(worker, task))["state"] == "completed"
    assert ol.offline_calls == ol.move_calls == ol.scan_calls == 1


async def test_cancel_preserves_downloaded_files(store):
    worker, task, ol, _ = new_worker(store)
    await drive(worker, task, terminal="organizing")
    await worker.cancel(task["id"])
    assert store.task(task["id"])["state"] == "canceled"
    assert ol.fs[store.task(task["id"])["payload"]["staging"]]


async def test_share_waits_for_entire_nested_tree(store):
    worker, task, ol, p115 = new_worker(store, "share")
    p115.share_entries=AsyncMock(return_value=[{"id":"123","name":"Season 1","is_dir":True,"size":0}])
    p115.share_tree=AsyncMock(return_value=[{"path":"Season 1/E01.mkv","size":200},
                                           {"path":"Season 1/E02.mkv","size":300},
                                           {"path":"Season 1/E02.srt","size":10}])
    await drive(worker, task, terminal="ready_submit")
    latest=store.task(task["id"])
    staging=latest["payload"]["staging"]
    store.update_task(task["id"],"transferring",accepted=True)
    ol.fs[staging]=[{"name":"Season 1","is_dir":True}]
    ol.fs[staging+'/Season 1']=[{"name":"E01.mkv","size":200,"is_dir":False}]
    await worker.step(store.task(task["id"]))
    assert store.task(task["id"])["state"] == 'transferring'
    ol.fs[staging+'/Season 1'] += [{"name":"E02.mkv","size":300,"is_dir":False},
                                  {"name":"E02.srt","size":10,"is_dir":False}]
    await worker.step(store.task(task["id"]))
    assert store.task(task["id"])["state"] == 'organizing'
    assert len(store.task(task["id"])["payload"]["videos"]) == 2


async def test_p115_qr_flow_and_identity_are_verified(store, monkeypatch):
    from medialink.clients import P115
    from p115client import P115Client
    vault=MemoryVault()
    openlist=SimpleNamespace(storage_identity=AsyncMock(return_value={"user_id":"123","root_id":"0"}))
    p115=P115(vault,store.settings,openlist)
    p115.settings_store=store
    monkeypatch.setattr(P115Client,'login_qrcode_token',AsyncMock(return_value={"data":{"uid":"test-uid","time":1,"sign":"test-sign"}}))
    status=AsyncMock(return_value={"data":{"status":2}})
    monkeypatch.setattr(P115Client,'login_qrcode_scan_status',status)
    result=AsyncMock(return_value={"data":{"cookie":{"UID":"123_A1_test","CID":"x","SEID":"x"}}})
    monkeypatch.setattr(P115Client,'login_qrcode_scan_result',result)
    p115.save_cookies=AsyncMock(return_value={"verified":True})
    await p115.qr_start()
    assert (await p115.qr_poll())['status'] == 'connected'
    status.assert_awaited_once_with({"uid":"test-uid","time":1,"sign":"test-sign"},async_=True,timeout=20)
    result.assert_awaited_once_with('test-uid',app='web',async_=True,timeout=20)
    assert p115.save_cookies.await_count == 1


def test_share_login_failure_does_not_log_out_application(client):
    authenticate(client)
    client.app.state.p115.share_entries=AsyncMock(side_effect=Problem('请先登录 115',409))
    response=client.post('/api/v1/tasks',json={"link":"https://115.com/s/example","target":"/115/infuse"})
    assert response.status_code == 409
    assert client.get('/api/v1/tasks').status_code == 200



def message(mid, body="星际穿越 1080p"):
    return SimpleNamespace(id=mid, raw_text=body, entities=[], reply_markup=None,
                           chat=SimpleNamespace(username="sample"), date=datetime.now(timezone.utc), edit_date=None)


async def test_telegram_backfill_cursor_and_restart_replay(store):
    tg = Telegram(store, MemoryVault())
    client = SimpleNamespace(get_entity=AsyncMock(return_value="channel"),
                             get_messages=AsyncMock(side_effect=[[message(200)], [message(i) for i in range(200,100,-1)],
                                                                [message(200)], [message(100)]]))
    async def iter_messages(*args, **kwargs):
        if False:
            yield
    client.iter_messages=iter_messages
    tg.connect=AsyncMock(return_value=client)
    await tg.sync_channel(store.channel("-1001"))
    assert store.channel("-1001")["cursor"] == 101
    reopened=Store(store.path)
    tg.store=reopened
    await tg.sync_channel(reopened.channel("-1001"))
    assert reopened.channel("-1001")["complete"] == 1
    assert reopened.channel("-1001")["count"] == 101


async def test_telegram_flood_wait_is_persisted(store, monkeypatch):
    tg=Telegram(store,MemoryVault())
    tg.authorized=AsyncMock(return_value=True)
    tg.sync_channel=AsyncMock(side_effect=errors.FloodWaitError(None,30))
    async def stop_sleep(seconds):
        tg.stopped=True
    monkeypatch.setattr("medialink.sources.asyncio.sleep",stop_sleep)
    await tg.run()
    assert store.channel("-1001")["wait_until"] > time.time()+28
    assert "限流" in store.channel("-1001")["status"]


@pytest.fixture
def client(store):
    app=create_app(store,MemoryVault(),start_workers=False)
    with TestClient(app) as client:
        client.app.state.openlist.get=AsyncMock(return_value={"is_dir":True})
        yield client


def authenticate(client):
    assert client.post('/api/v1/auth/setup',json={"password":"test-password-123"}).status_code == 200


def test_login_origin_and_private_api(client):
    assert client.get('/api/v1/tasks').status_code == 401
    authenticate(client)
    assert client.get('/api/v1/tasks').status_code == 200
    assert client.post('/api/v1/tasks',headers={"Origin":"https://evil.example"},json={}).status_code == 403
    assert client.get('/api/v1/tasks',headers={"Host":"evil.example"}).status_code == 403
    client.post('/api/v1/auth/logout')
    assert client.get('/api/v1/tasks').status_code == 401
    assert client.post('/api/v1/auth/login',json={"password":"wrong-password"}).status_code == 401
    assert client.post('/api/v1/auth/login',json={"password":"test-password-123"}).status_code == 200


def test_public_hostname_allowlist(client, monkeypatch):
    monkeypatch.delenv('MEDIALINK_ALLOWED_HOSTS', raising=False)
    headers = {"Host": "media.example.com:8766"}
    assert client.get('/api/v1/health', headers=headers).status_code == 403
    authenticate(client)
    assert client.patch('/api/v1/settings', json={"public_access_enabled": True,
                                                "public_access_hosts": "media.example.com"}).status_code == 200
    assert client.get('/api/v1/health', headers=headers).status_code == 200
    assert client.get('http://media.example.com:8766/api/v1/tasks').status_code == 401
    assert client.post('/api/v1/auth/login', headers={**headers, "Origin": "http://media.example.com:8766"},
                       json={"password": "test-password-123"}).status_code == 200
    assert client.post('/api/v1/auth/login', headers={**headers, "Origin": "https://evil.example"},
                       json={"password": "test-password-123"}).status_code == 403
    assert client.get('/api/v1/health', headers={"Host": "evil.example"}).status_code == 403


def test_api_duplicate_submit_secrets_not_exposed_and_path_rejection(client):
    authenticate(client)
    payload={"link":MAGNET,"target":"/115/infuse","title":"Test"}
    first=client.post('/api/v1/tasks',json=payload)
    assert first.status_code == 200
    second=client.post('/api/v1/tasks',json=payload)
    assert first.json()['id'] == second.json()['id']
    assert MAGNET not in first.text and 'link_enc' not in first.text
    assert client.post('/api/v1/tasks',json={**payload,"target":"/115/infuse/../private"}).status_code == 422
    assert client.post('/api/v1/tasks',json={**payload,"link":"password-secret","password":[]}).status_code == 422
    invalid=client.post('/api/v1/tasks',json={**payload,"password":[]})
    assert 'input' not in invalid.text


def test_sse_partial_failure_and_cross_source_dedup(client):
    authenticate(client)
    resource=extract_resources('Movie',[MAGNET],{"provider":"telegram","name":"TG","url":"https://t.me/x/1"})
    client.app.state.nyaa.search=AsyncMock(side_effect=Problem('Nyaa unavailable',502))
    client.app.state.telegram.search=AsyncMock(return_value=resource)
    response=client.post('/api/v1/search',json={"query":"Movie","sources":["nyaa","telegram"]})
    search_id=response.json()['id']
    events=client.get('/api/v1/search/'+search_id+'/events').text
    assert 'Nyaa unavailable' in events and 'event: done' in events and 'Movie' in events


def test_settings_secrets_only_in_vault_and_changes_blocked_during_tasks(client):
    authenticate(client)
    settings=client.get('/api/v1/settings').json()
    result=client.patch('/api/v1/settings',json={"telegram_api_hash":"secret-hash-123","telegram_api_id":"123456"})
    assert result.status_code == 200 and 'secret-hash-123' not in result.text
    client.post('/api/v1/tasks',json={"link":MAGNET,"target":"/115/infuse"})
    assert client.patch('/api/v1/settings',json={"media_root":"/115/another","last_target":"/115/another"}).status_code == 409
    assert client.patch('/api/v1/settings',json={"media_root":settings['media_root']}).status_code == 200


@pytest.mark.asyncio
async def test_strm_single_source_flattening_checks_actual_path(store, tmp_path):
    import sqlite3

    from medialink.clients import OpenList
    database = tmp_path / 'openlist.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE x_storages(mount_path,addition,driver)')
        db.execute('INSERT INTO x_storages VALUES (?,?,?)',
                   ('/115Strm', json.dumps({'paths':'/115/infuse'}), 'Strm'))
    store.configure({'openlist_db':str(database)})
    client = OpenList(store.settings, MemoryVault())
    async def get(path):
        if path.startswith('/115Strm/infuse'):
            raise RemoteFailure('not found')
        return {'is_dir':True}
    client.get = AsyncMock(side_effect=get)
    client.request = AsyncMock()
    assert await client.scan('/115Strm/infuse/测试') == '/115Strm/测试'
    client.request.assert_awaited_once_with('POST', '/api/admin/scan/start', {'path':'/115Strm/测试','limit':2})
    assert client.get.await_args.args == ('/115Strm/测试',)
    # Existing unflattened mappings retain their configured path.
    client.get = AsyncMock(return_value={'is_dir':True})
    assert await client.resolve_strm_path('/115Strm/infuse/测试') == '/115Strm/infuse/测试'
    # Do not guess a path when there are multiple source mappings.
    with sqlite3.connect(database) as db:
        db.execute('UPDATE x_storages SET addition=?', (json.dumps({'paths':'/115/infuse\n/other/media'}),))
    client.get = AsyncMock(side_effect=RemoteFailure('not found'))
    with pytest.raises(Problem, match='STRM'):
        await client.resolve_strm_path('/115Strm/infuse/测试')


@pytest.mark.asyncio
async def test_share_receive_keeps_embedded_extraction_code(store):
    from medialink.clients import P115
    client = P115(MemoryVault(), store.settings, FakeOpenList())
    client.call = AsyncMock()
    await client.receive('https://115.com/s/abc123?password=aB12', '', ['1','2'], '999')
    assert client.call.await_args.args[1]['receive_code'] == 'aB12'
    await client.receive('https://115.com/s/abc123?password=aB12', 'cD34', ['1'], '999')
    assert client.call.await_args.args[1]['receive_code'] == 'cD34'


@pytest.mark.parametrize('logged_in,uid,expected', [(True,'123',None),(False,'123','已失效'),(True,'999','不同')])
async def test_115_login_authenticates_cookie_owner(store, monkeypatch, logged_in, uid, expected):
    from medialink.clients import P115
    from p115client import P115Client
    vault = MemoryVault()
    ol = SimpleNamespace(storage_identity=AsyncMock(return_value={'user_id':'123','root_id':'0'}))
    p115 = P115(vault, store.settings, ol)
    p115.settings_store = store
    monkeypatch.setattr(P115Client,'login_status',AsyncMock(return_value=logged_in))
    # A public profile lookup must never decide the authenticated account owner.
    profile = AsyncMock(return_value={'state':True,'data':{'user_id':'11500'}})
    monkeypatch.setattr(P115Client,'user_info',profile)
    cookies = f'UID={uid}_A1_test; CID=test; SEID=test'
    if expected:
        with pytest.raises(Problem,match=expected):
            await p115.save_cookies(cookies)
        assert vault.get('115-cookies') is None
        assert not store.settings().get('115_account_verified')
    else:
        await p115.save_cookies(cookies)
        assert vault.get('115-cookies') == cookies
        assert store.settings()['115_account_verified'] == uid
    profile.assert_not_awaited()


async def test_115_qr_upstream_denial_is_not_hidden_as_waiting(store, monkeypatch):
    from medialink.clients import P115
    from p115client import P115Client
    p115 = P115(MemoryVault(), store.settings, FakeOpenList())
    p115.qr = {'uid':'test','time':1,'sign':'secret'}
    monkeypatch.setattr(P115Client,'login_qrcode_scan_status',AsyncMock(return_value={'data':{'status':2}}))
    monkeypatch.setattr(P115Client,'login_qrcode_scan_result',AsyncMock(return_value={
        'state':False,'code':99001,'message':'IP登录异常 secret-cookie-value'}))
    with pytest.raises(Problem,match='IP 登录异常') as error:
        await p115.qr_poll()
    assert 'secret-cookie-value' not in error.value.message
    assert p115.qr_cookies is None


async def test_115_qr_retry_reuses_confirmed_credentials(store, monkeypatch):
    from medialink.clients import P115
    from p115client import P115Client
    p115 = P115(MemoryVault(), store.settings, FakeOpenList())
    p115.qr = {'uid':'test','time':1,'sign':'secret'}
    monkeypatch.setattr(P115Client,'login_qrcode_scan_status',AsyncMock(return_value={'data':{'status':2}}))
    result = AsyncMock(return_value={'data':{'cookie':{'UID':'123_A1_test','CID':'test','SEID':'test'}}})
    monkeypatch.setattr(P115Client,'login_qrcode_scan_result',result)
    p115.save_cookies = AsyncMock(side_effect=[Problem('OpenList 暂时不可用',503),{'verified':True}])
    with pytest.raises(Problem):
        await p115.qr_poll()
    assert p115.qr_cookies
    assert (await p115.qr_poll())['status'] == 'connected'
    assert result.await_count == 1
    assert p115.qr is None and p115.qr_cookies is None


async def test_115_qr_overlapping_polls_consume_result_once(store, monkeypatch):
    import asyncio

    from medialink.clients import P115
    from p115client import P115Client
    p115 = P115(MemoryVault(), store.settings, FakeOpenList())
    p115.qr = {'uid':'test','time':1,'sign':'secret'}
    monkeypatch.setattr(P115Client,'login_qrcode_scan_status',AsyncMock(return_value={'data':{'status':2}}))
    result = AsyncMock(return_value={'data':{'cookie':{'UID':'123_A1_test','CID':'test','SEID':'test'}}})
    monkeypatch.setattr(P115Client,'login_qrcode_scan_result',result)
    async def save(cookies):
        await asyncio.sleep(0)
        p115.client = SimpleNamespace()
    p115.save_cookies = save
    assert await asyncio.gather(p115.qr_poll(),p115.qr_poll()) == [{'status':'connected'},{'status':'connected'}]
    assert result.await_count == 1


def test_directory_listing_reports_media_root_from_any_folder(client):
    authenticate(client)
    client.app.state.openlist.entries = AsyncMock(return_value=[{'name':'2024','is_dir':True},{'name':'a.mkv','is_dir':False}])
    data = client.get('/api/v1/directories',params={'path':'/115/infuse/欧美电影'}).json()
    assert data == {'path':'/115/infuse/欧美电影','root':'/115/infuse',
                    'directories':[{'name':'2024','path':'/115/infuse/欧美电影/2024'}]}


def test_directory_creation_and_invalid_or_conflicting_names(client):
    authenticate(client)
    ol = client.app.state.openlist
    ol.entries = AsyncMock(return_value=[{'name':'电影','is_dir':True},{'name':'existing.txt','is_dir':False}])
    ol.mkdir = AsyncMock()
    for name in ['', '.', '..', 'a/b', 'a\\b', 'a\nname', None]:
        assert client.post('/api/v1/directories',json={'parent':'/115/infuse','name':name}).status_code == 422
    assert client.post('/api/v1/directories',json={'parent':'/115/infuse/../private','name':'新建'}).status_code == 422
    for name in ['电影','existing.txt','EXISTING.TXT']:
        assert client.post('/api/v1/directories',json={'parent':'/115/infuse','name':name}).status_code == 409
    ol.mkdir.assert_not_awaited()
    response = client.post('/api/v1/directories',json={'parent':'/115/infuse','name':' 新建目录 '})
    assert response.status_code == 200 and response.json()['path'] == '/115/infuse/新建目录'
    ol.mkdir.assert_awaited_once_with('/115/infuse/新建目录')


def test_directory_rename_preserves_selection_and_rejects_collisions(client):
    authenticate(client)
    ol, store = client.app.state.openlist, client.app.state.store
    ol.entries = AsyncMock(return_value=[{'name':'旧名称','is_dir':True},{'name':'已有名称','is_dir':True},
                                         {'name':'文件.txt','is_dir':False}])
    ol.rename = AsyncMock()
    store.configure({'last_target':'/115/infuse/旧名称/第二季'})
    assert client.patch('/api/v1/directories',json={'path':'/115/infuse','name':'媒体'}).status_code == 422
    assert client.patch('/api/v1/directories',json={'path':'/115/infuse-other/旧名称','name':'新名称'}).status_code == 422
    assert client.patch('/api/v1/directories',json={'path':'/115/infuse/文件.txt','name':'新名称'}).status_code == 404
    assert client.patch('/api/v1/directories',json={'path':'/115/infuse/旧名称','name':'已有名称'}).status_code == 409
    ol.rename.assert_not_awaited()
    response = client.patch('/api/v1/directories',json={'path':'/115/infuse/旧名称','name':'新名称'})
    assert response.status_code == 200
    ol.rename.assert_awaited_once_with('/115/infuse/旧名称','新名称')
    assert response.json()['last_target'] == '/115/infuse/新名称/第二季'
    assert store.settings()['last_target'] == '/115/infuse/新名称/第二季'


def test_directory_rename_blocks_active_descendants_but_not_similar_prefix(client):
    authenticate(client)
    ol, store = client.app.state.openlist, client.app.state.store
    ol.entries = AsyncMock(return_value=[{'name':'电影','is_dir':True}])
    ol.rename = AsyncMock()
    unrelated = store.create_task('magnet:'+'b'*40,'/115/infuse/电影合集',{'kind':'magnet'})
    task = store.create_task('magnet:'+'c'*40,'/115/infuse/电影/第二季',{'kind':'magnet'})
    for state in ['queued','moving','needs_attention']:
        store.update_task(task['id'],state)
        assert client.patch('/api/v1/directories',json={'path':'/115/infuse/电影','name':'新名称'}).status_code == 409
    ol.rename.assert_not_awaited()
    store.update_task(task['id'],'canceled')
    assert client.patch('/api/v1/directories',json={'path':'/115/infuse/电影','name':'新名称'}).status_code == 200
    assert store.task(unrelated['id'])['state'] == 'queued'
    store.create_task('magnet:'+'d'*40,'/115/infuse',{'kind':'magnet'})
    assert client.patch('/api/v1/directories',json={'path':'/115/infuse/电影','name':'新名称'}).status_code == 409


def test_directory_rename_timeout_does_not_repeat_write(client):
    authenticate(client)
    ol = client.app.state.openlist
    ol.entries = AsyncMock(return_value=[{'name':'原目录','is_dir':True}])
    ol.rename = AsyncMock(side_effect=RemoteFailure('timeout',uncertain=True))
    response = client.patch('/api/v1/directories',json={'path':'/115/infuse/原目录','name':'新目录'})
    assert response.status_code == 502 and '待确认' in response.json()['detail']
    assert ol.rename.await_count == 1


async def test_openlist_rename_never_overwrites(store):
    from medialink.clients import OpenList
    ol = OpenList(store.settings,MemoryVault())
    ol.request = AsyncMock()
    await ol.rename('/115/infuse/old','new')
    ol.request.assert_awaited_once_with('POST','/api/fs/rename',{'path':'/115/infuse/old','name':'new','overwrite':False})
