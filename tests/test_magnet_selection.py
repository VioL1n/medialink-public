import copy
import json
import time
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock

import pytest
from medialink.clients import RemoteFailure
from medialink.core import Problem, Store, resource_key
from medialink.magnets import preview_snapshot
from medialink.selection import map_selection, single_file_plan
from medialink.worker import Worker
from test_medialink import MAGNET, FakeOpenList, FakeP115, MemoryVault, drive
from test_naming import TV

pytest_plugins = ["test_medialink"]
TREE = [{"path": "合集/第一季/Show.S01E01.1080p.mkv", "size": 100},
        {"path": "合集/第一季/Show.S01E01.1080p.zh.srt", "size": 30},
        {"path": "合集/第一季/Unselected.S01E02E03.mkv", "size": 200},
        {"path": "合集/第二季/Show.S02E01.1080p.mkv", "size": 150},
        {"path": "合集/readme.txt", "size": 10}]


def selection():
    preview = preview_snapshot("a" * 40, "合集", TREE)
    chosen = [preview["files"][index] for index in (0, 1, 3)]
    return {"preview_id": preview["preview_id"], "root": preview["root"], "files": chosen,
            "selected_ids": [item["id"] for item in chosen], "total_count": len(TREE)}


class TreeOpenList(FakeOpenList):
    def __init__(self, strip=False):
        super().__init__()
        self.strip = strip
        self.timeout_selection = self.partial_selection = False
        self.selection_calls = []

    async def mkdir(self, path):
        if path in self.fs:
            return
        parent = str(PurePosixPath(path).parent)
        if parent != path:
            await self.mkdir(parent)
            self.fs[parent].append({"name": PurePosixPath(path).name, "is_dir": True})
        self.fs[path] = []

    async def offline(self, link, path):
        self.offline_calls += 1
        self.remote.append({"id": "mine", "name": path, "state": 2, "bucket": "done", "progress": 100})
        for i, item in enumerate(TREE):
            relative = item["path"].split("/", 1)[1] if self.strip else item["path"]
            target = PurePosixPath(path) / relative
            await self.mkdir(str(target.parent))
            self.fs[str(target.parent)].append({"name": target.name, "size": item["size"], "id": str(i), "is_dir": False})
        return "mine"

    async def move(self, src, dst, names):
        is_selection = "-selected" in dst
        self.move_calls += 1
        if is_selection:
            self.selection_calls.append((src, dst, names))
        if any(entry["name"] in names for entry in self.fs[dst]):
            raise RemoteFailure("collision")
        for name in names[:1] if is_selection and self.partial_selection else names:
            item = next(entry for entry in self.fs[src] if entry["name"] == name)
            self.fs[src].remove(item)
            self.fs[dst].append(item)
            if item.get("is_dir"):
                prefix, target = src + "/" + name, dst + "/" + name
                for directory in list(self.fs):
                    if directory == prefix or directory.startswith(prefix + "/"):
                        self.fs[target + directory[len(prefix):]] = self.fs.pop(directory)
        if is_selection and (self.timeout_selection or self.partial_selection):
            raise RemoteFailure("timeout", uncertain=True)

    async def rename(self, path, name):
        p = PurePosixPath(path)
        entries = self.fs[str(p.parent)]
        assert not any(entry["name"] == name for entry in entries)
        next(entry for entry in entries if entry["name"] == p.name)["name"] = name


def create_worker(store, strip=False, naming=False):
    vault, ol = MemoryVault(), TreeOpenList(strip)
    worker = Worker(store, vault, ol, FakeP115(ol))
    payload = {"kind": "magnet", "title": "选中文件", "link_enc": vault.seal(json.dumps({"link": MAGNET})),
               "magnet_selection": selection(), "native_size": 490}
    if naming:
        payload["naming"] = {"version": 1, "media": TV, "files": [], "plan": [], "overrides": {}, "locked": False}
    task = store.create_task(resource_key(MAGNET), "/115/infuse", payload)
    return worker, task, ol


@pytest.mark.parametrize("strip", [False, True])
@pytest.mark.parametrize("naming", [False, True])
async def test_selected_only_ingest_and_strm_preserves_unselected(store, monkeypatch, strip, naming):
    worker, task, ol = create_worker(store, strip, naming)
    notify = AsyncMock()
    monkeypatch.setattr("medialink.worker.refresh_emby", notify)
    result = await drive(worker, task, "scanning", iterations=100)
    assert result["state"] == "scanning"
    original = result["payload"]["download_staging"]
    assert result["payload"]["retained_count"] == 2
    remains = await worker.file_manifest(original)
    assert {item["size"] for item in remains} == {200, 10}
    assert any("Unselected.S01E02E03.mkv" in item["path"] for item in remains)
    imported = await worker.file_manifest("/115/infuse")
    assert {item["size"] for item in imported} == {100, 30, 150}
    assert len(imported) == 3
    assert not any("Unselected" in item["path"] or "readme" in item["path"] for item in imported)
    assert (not strip) == all(item["path"].startswith("合集/") for item in imported)
    assert len(ol.selection_calls) == 3
    assert all(len(names) == 1 for _, _, names in ol.selection_calls)
    assert all(not any("Unselected" in name for name in names) for _, _, names in ol.selection_calls)
    if naming:
        assert any("绝命毒师 S01E01.mkv" in item["path"] for item in imported)
        assert any("绝命毒师 S02E01.mkv" in item["path"] for item in imported)
        assert any("绝命毒师 S01E01.zh.srt" in item["path"] for item in imported)
    await worker.step(result)
    notify.assert_not_awaited()
    videos = result["payload"]["videos"]
    assert len(videos) == 2
    for video in videos:
        strm = Path(store.settings()["strm_local"]) / "infuse" / PurePosixPath(video).with_suffix(".strm")
        strm.parent.mkdir(parents=True, exist_ok=True)
        strm.write_text("selected")
    assert (await drive(worker, result))["state"] == "completed"
    notify.assert_awaited_once()
    assert ol.offline_calls == 1


@pytest.mark.parametrize("issue", ["missing", "size", "ambiguous"])
def test_selection_mapping_never_guesses(issue):
    files = [{**item, "identity": {}} for item in TREE]
    if issue == "missing":
        files = files[1:]
    elif issue == "size":
        files[0]["size"] += 1
    else:
        files += [{**item, "path": item["path"].split("/", 1)[1]} for item in files]
    with pytest.raises(Problem, match="唯一匹配"):
        map_selection(selection(), files)


async def test_selection_timeout_restart_reconciles_without_repeat(store):
    worker, task, ol = create_worker(store)
    task = await drive(worker, task, "selecting_files")
    await worker.step(task)  # Initialize the isolated destination.
    ol.timeout_selection = True
    await worker.step(store.task(task["id"]))
    assert len(ol.selection_calls) == 1
    worker.store = Store(store.path)
    await worker.step(worker.store.task(task["id"]))
    assert len(ol.selection_calls) == 1
    ol.timeout_selection = False
    result = await drive(worker, task, "selected_ready")
    assert result["state"] == "selected_ready"
    assert len(ol.selection_calls) == 3 and ol.offline_calls == 1


@pytest.mark.parametrize("issue", ["identity", "unknown_identity", "source_present", "collision"])
async def test_uncertain_selection_does_not_move_or_overwrite_again(store, issue):
    worker, task, ol = create_worker(store)
    task = await drive(worker, task, "selecting_files")
    await worker.step(task)
    task = store.task(task["id"])
    first = task["payload"]["selection_plan"][0]
    dst = str(PurePosixPath(task["payload"]["selection_staging"]) / first["directory"])
    if issue == "collision":
        ol.fs[dst].append({"name": PurePosixPath(first["files"][0]["path"]).name, "size": 100, "id": "someone-else"})
        with pytest.raises(Problem, match="同名"):
            await worker.step(task)
        assert not ol.selection_calls
        return
    if issue == "unknown_identity":
        for item in first["files"]:
            item["identity"] = {}
        worker.update(task, selection_plan=task["payload"]["selection_plan"])
    if issue == "source_present":
        ol.move = AsyncMock(side_effect=RemoteFailure("timeout", uncertain=True))
    else:
        ol.timeout_selection = True
    await worker.step(store.task(task["id"]))
    task = store.task(task["id"])
    if issue == "identity":
        ol.fs[dst][0]["id"] = "different"
    task["payload"]["selection_plan"][0]["attempted_at"] = time.time() - 181
    worker.update(task, selection_plan=task["payload"]["selection_plan"])
    with pytest.raises(Problem, match="无法确认"):
        await worker.step(store.task(task["id"]))
    assert ol.move_calls <= 1
    assert not ol.fs["/115/infuse"]


async def test_unselected_mutation_or_selected_injection_pauses(store):
    worker, task, ol = create_worker(store)
    task = await drive(worker, task, "selecting_files")
    await worker.step(task)
    for _ in range(2 * len(task["payload"]["selection_plan"])):
        await worker.step(store.task(task["id"]))
    task = store.task(task["id"])
    original = task["payload"]["download_staging"]
    ol.fs[original].append({"name": "new.txt", "size": 1})
    with pytest.raises(Problem, match="未选中"):
        await worker.step(task)
    ol.fs[original].pop()
    task = await drive(worker, task, "selected_ready")
    ol.fs[task["payload"]["staging"]].append({"name": "not-selected.mkv", "size": 50})
    with pytest.raises(Problem, match="已变化"):
        await worker.step(task)
    assert not ol.fs["/115/infuse"]


@pytest.mark.parametrize("issue", ["empty", "stale_transfer"])
async def test_selected_ready_uses_verified_snapshot_instead_of_old_transfer(store, issue):
    worker, task, ol = create_worker(store)
    task = await drive(worker, task, "selected_ready")
    if issue == "empty":
        ol.fs[task["payload"]["staging"]] = []
        with pytest.raises(Problem, match="为空"):
            await worker.step(task)
    else:
        task = worker.update(task, transfer_id="removed-transfer-task")
        ol.request = AsyncMock(side_effect=AssertionError("completed transfer must not be queried again"))
        await worker.step(task)
        assert store.task(task["id"])["state"] == "organizing"
        ol.request.assert_not_awaited()


@pytest.mark.parametrize("status", ["pending", "attempted", "done"])
def test_legacy_batch_splits_without_losing_evidence(status):
    files = [{**item, "identity": {"id": str(i)}} for i, item in enumerate(TREE[:2])]
    plan = [{"directory": "合集/第一季", "files": files, "status": status,
             "attempted_at": 123, "acknowledged": True}]
    original = copy.deepcopy(plan)
    split = single_file_plan(plan)
    assert len(split) == 2 and plan == original
    assert [group["files"][0] for group in split] == files
    assert all(group["status"] == status and group["attempted_at"] == 123
               and group["acknowledged"] is True for group in split)


async def test_twelve_episodes_move_individually_with_cloud_hashes(store, monkeypatch):
    import hashlib

    tree = [{"path": f"合集/Show.S01E{i:02}.mkv", "size": 100 + i} for i in range(1, 13)]
    tree.append({"path": "合集/readme.txt", "size": 10})
    monkeypatch.setattr("test_magnet_selection.TREE", tree)
    offline = TreeOpenList.offline

    async def cloud_offline(self, link, path):
        result = await offline(self, link, path)
        for entries in self.fs.values():
            for entry in entries:
                if "id" in entry:
                    entry["hash_info"] = {"sha1": hashlib.sha1(entry.pop("id").encode()).hexdigest().upper()}
        return result

    monkeypatch.setattr(TreeOpenList, "offline", cloud_offline)
    worker, task, ol = create_worker(store)
    preview = preview_snapshot("a" * 40, "合集", tree)
    chosen = preview["files"][:12]
    task = worker.update(task, native_size=sum(item["size"] for item in tree), magnet_selection={
        "preview_id": preview["preview_id"], "root": preview["root"], "files": chosen,
        "selected_ids": [item["id"] for item in chosen], "total_count": len(tree)})
    result = await drive(worker, task, "scanning", iterations=100)
    assert result["state"] == "scanning" and result["payload"]["retained_count"] == 1
    assert len(ol.selection_calls) == 12 and all(len(names) == 1 for _, _, names in ol.selection_calls)
    assert len(await worker.file_manifest(task["target"])) == 12
    remains = await worker.file_manifest(result["payload"]["download_staging"])
    assert [item["path"] for item in remains] == ["合集/readme.txt"]
    assert ol.offline_calls == 1


async def paused_attempt(worker, task, *, legacy=False, age=181):
    task = await drive(worker, task, "selecting_files")
    await worker.step(task)
    task = worker.store.task(task["id"])
    plan = task["payload"]["selection_plan"]
    if legacy:
        plan = [{**plan[0], "files": plan[0]["files"] + plan[1]["files"]}, *plan[2:]]
    plan[0].update(status="attempted", attempted_at=time.time() - age)
    return worker.update(task, "needs_attention", resume_state="selecting_files",
                         error="选中文件移动结果无法确认", selection_plan=plan)


@pytest.mark.parametrize("legacy", [False, True])
async def test_explicit_retry_unchanged_move_resumes_without_new_download(store, legacy):
    worker, task, ol = create_worker(store)
    task = await paused_attempt(worker, task, legacy=legacy)
    # Service restarts leave paused tasks alone and retain their operation evidence.
    worker.store = Store(store.path)
    assert not worker.store.tasks(active=True)
    assert worker.store.task(task["id"])["payload"]["selection_plan"] == task["payload"]["selection_plan"]
    resumed = await worker.retry(task["id"])
    assert resumed["state"] == "selecting_files"
    assert len(resumed["payload"]["selection_plan"]) == 3
    assert all(group["status"] == "pending" for group in resumed["payload"]["selection_plan"])
    assert ol.move_calls == 0
    # A second restart after authorizing the retry does not lose the new plan.
    worker.store = Store(store.path)
    result = await drive(worker, resumed, "selected_ready")
    assert result["state"] == "selected_ready" and len(ol.selection_calls) == 3
    assert all(len(names) == 1 for _, _, names in ol.selection_calls)
    assert ol.offline_calls == 1


@pytest.mark.parametrize("action", ["retry", "step"])
async def test_legacy_partial_batch_reconciles_each_file_without_repeating(store, action):
    worker, task, ol = create_worker(store)
    task = await paused_attempt(worker, task, legacy=True)
    group = task["payload"]["selection_plan"][0]
    source = str(PurePosixPath(task["payload"]["download_staging"]) / group["directory"])
    target = str(PurePosixPath(task["payload"]["selection_staging"]) / group["directory"])
    ol.partial_selection = True
    with pytest.raises(RemoteFailure):
        await ol.move(source, target, [PurePosixPath(item["path"]).name for item in group["files"]])
    ol.partial_selection = False
    if action == "step":
        task = worker.update(task, "selecting_files")
        await worker.step(task)
        task = store.task(task["id"])
        assert task["payload"]["selection_plan"][0]["status"] == "done"
        with pytest.raises(Problem, match="无法确认"):
            await worker.step(task)
        assert ol.move_calls == 1
        task = worker.update(store.task(task["id"]), "needs_attention", resume_state="selecting_files")
    resumed = await worker.retry(task["id"])
    assert resumed["payload"]["selection_plan"][0]["status"] == "done"
    assert resumed["payload"]["selection_plan"][1]["status"] == "pending"
    assert ol.move_calls == 1
    result = await drive(worker, resumed, "selected_ready")
    assert result["state"] == "selected_ready" and len(ol.selection_calls) == 3
    first = PurePosixPath(group["files"][0]["path"]).name
    assert all(first not in names for _, _, names in ol.selection_calls[1:])


@pytest.mark.parametrize("identity", [True, False])
async def test_explicit_retry_already_moved_only_reconciles(store, identity):
    worker, task, ol = create_worker(store)
    task = await paused_attempt(worker, task, legacy=True)
    plan = task["payload"]["selection_plan"]
    group = plan[0]
    if not identity:
        for item in group["files"]:
            item["identity"] = {}
        group["acknowledged"] = True
        task = worker.update(task, selection_plan=plan)
    source = str(PurePosixPath(task["payload"]["download_staging"]) / group["directory"])
    target = str(PurePosixPath(task["payload"]["selection_staging"]) / group["directory"])
    await ol.move(source, target, [PurePosixPath(item["path"]).name for item in group["files"]])
    resumed = await worker.retry(task["id"])
    assert [group["status"] for group in resumed["payload"]["selection_plan"]] == ["done", "done", "pending"]
    assert ol.move_calls == 1


@pytest.mark.parametrize("issue", ["collision", "identity", "size", "unknown_identity", "both", "missing", "young"])
async def test_retry_unverifiable_move_stays_paused_without_writes(store, issue):
    worker, task, ol = create_worker(store)
    task = await paused_attempt(worker, task, age=30 if issue == "young" else 181)
    group = task["payload"]["selection_plan"][0]
    item = group["files"][0]
    source = str(PurePosixPath(task["payload"]["download_staging"]) / group["directory"])
    target = str(PurePosixPath(task["payload"]["selection_staging"]) / group["directory"])
    entry = next(e for e in ol.fs[source] if e["name"] == PurePosixPath(item["path"]).name)
    if issue in {"collision", "both"}:
        ol.fs[target].append({**entry, "id": "foreign" if issue == "collision" else entry["id"]})
    elif issue == "identity":
        entry["id"] = "changed"
    elif issue == "size":
        entry["size"] += 1
    elif issue == "unknown_identity":
        item["identity"] = {}
        task = worker.update(task, selection_plan=task["payload"]["selection_plan"])
    elif issue == "missing":
        ol.fs[source].remove(entry)
    before = store.task(task["id"])
    remote_before = copy.deepcopy(ol.fs)
    with pytest.raises(Problem) as exc:
        await worker.retry(task["id"])
    assert exc.value.code == 409
    assert store.task(task["id"]) == before
    assert ol.fs == remote_before and ol.move_calls == 0 and ol.offline_calls == 1


async def test_retry_validation_is_atomic_across_legacy_batch(store):
    worker, task, ol = create_worker(store)
    task = await paused_attempt(worker, task, legacy=True)
    group = task["payload"]["selection_plan"][0]
    target = str(PurePosixPath(task["payload"]["selection_staging"]) / group["directory"])
    ol.fs[target].append({"name": PurePosixPath(group["files"][1]["path"]).name,
                          "size": group["files"][1]["size"], "id": "foreign"})
    before = store.task(task["id"])
    with pytest.raises(Problem, match="无法确认"):
        await worker.retry(task["id"])
    assert store.task(task["id"]) == before and ol.move_calls == 0


async def test_retry_completed_legacy_batch_keeps_done_files(store):
    worker, task, ol = create_worker(store)
    task = await drive(worker, task, "selected_ready")
    plan = task["payload"]["selection_plan"]
    plan = [{**plan[0], "files": plan[0]["files"] + plan[1]["files"]}, *plan[2:]]
    task = worker.update(task, "needs_attention", resume_state="selecting_files", selection_plan=plan)
    result = await worker.retry(task["id"])
    result = await drive(worker, result, "selected_ready")
    assert result["state"] == "selected_ready" and len(ol.selection_calls) == 3


@pytest.mark.parametrize("outcome", ["slow_success", "timeout"])
async def test_openlist_move_has_dedicated_timeout_and_no_automatic_retry(store, monkeypatch, outcome):
    import httpx
    from medialink.clients import OpenList

    requests = []

    async def handler(request):
        requests.append(request)
        if request.url.path == "/api/fs/move":
            # Model a 40-second upstream response against HTTPX's actual timeout configuration.
            if outcome == "timeout" or request.extensions["timeout"]["read"] < 40:
                raise httpx.ReadTimeout("delayed response", request=request)
            assert json.loads(request.content)["overwrite"] is False
        return httpx.Response(200, json={"code": 200, "data": {"content": []}})

    client_type = httpx.AsyncClient
    monkeypatch.setattr("medialink.clients.httpx.AsyncClient",
                        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs))
    vault = MemoryVault()
    vault.set("openlist-token", "test-token")
    ol = OpenList(store.settings, vault)
    await ol.entries("/115/source")
    if outcome == "timeout":
        with pytest.raises(RemoteFailure) as exc:
            await ol.move("/115/source", "/115/target", ["episode.mkv"])
        assert exc.value.uncertain is True
    else:
        await ol.move("/115/source", "/115/target", ["episode.mkv"])
    await ol.rename("/115/source/episode.mkv", "renamed.mkv")
    assert [request.extensions["timeout"]["read"] for request in requests] == [25, 120, 25]
    assert all(request.extensions["timeout"]["connect"] == 25 for request in requests)
    assert len(requests) == 3
