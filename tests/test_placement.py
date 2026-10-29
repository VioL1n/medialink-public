import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from medialink.clients import RemoteFailure
from medialink.core import Problem, Store, resource_key
from medialink.magnets import preview_snapshot
from medialink.naming import file_id
from medialink.worker import Worker
from test_curation import AD, GB, JACKPOT, MB, TV
from test_magnet_selection import TreeOpenList
from test_medialink import MAGNET, FakeP115, MemoryVault, drive

pytest_plugins = ["test_medialink"]
TARGET = "/115/infuse/电视剧"
FOLDER = "中头奖还是要上班 (2024) [tmdbid=99]"
SEASON = f"{TARGET}/{FOLDER}/Season 01"
RELEASE = [{"path": f"{JACKPOT}/The.Ordinary.Jackpot.S01E0{e}.1080p.WEB-DL.mkv", "size": 1300 * MB} for e in (1, 2)] + [
    {"path": f"{JACKPOT}/The.Ordinary.Jackpot.S01E02.1080p.WEB-DL.chs.ass", "size": 40_000},
    {"path": f"{JACKPOT}/{AD}.MP4", "size": 6 * MB}, {"path": f"{JACKPOT}/更多资源.url", "size": 1}]


class LibraryOpenList(TreeOpenList):
    def __init__(self, tree=RELEASE):
        super().__init__()
        self.tree, self.library_moves, self.timeout_library = tree, [], False

    async def offline(self, link, path):
        self.offline_calls += 1
        self.remote.append({"id": "mine", "name": path, "state": 2, "bucket": "done", "progress": 100})
        for i, item in enumerate(self.tree):
            target = PurePosixPath(path) / item["path"]
            await self.mkdir(str(target.parent))
            self.fs[str(target.parent)].append({"name": target.name, "size": item["size"], "id": f"cloud-{i}", "is_dir": False})
        return "mine"

    async def move(self, src, dst, names):
        library = dst.startswith(TARGET)
        if library:
            self.library_moves.append((src, dst, names))
        await super().move(src, dst, names)
        if library and self.timeout_library:
            raise RemoteFailure("timeout", uncertain=True)


def create(store, media=TV, tree=RELEASE, llm=None, **payload):
    vault, ol = MemoryVault(), LibraryOpenList(tree)
    worker = Worker(store, vault, ol, FakeP115(ol), llm=llm)
    naming = {"version": 2, "media": media, "season_number": None, "resolution": None, "files": [], "plan": [],
              "overrides": {}, "locked": False}
    task = store.create_task(resource_key(MAGNET), TARGET, {"kind": "magnet", "title": "中头奖还是要上班",
                             "link_enc": vault.seal(json.dumps({"link": MAGNET})), "naming": naming, "pipeline": 2,
                             **payload})
    return worker, task, ol


def write_strm(store, task):
    for video in task["payload"]["videos"]:
        path = Path(store.settings()["strm_local"]) / "infuse/电视剧" / PurePosixPath(video).with_suffix(".strm")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stream")


async def test_ads_stay_in_staging_and_title_folder_reaches_emby(store, monkeypatch):
    worker, task, ol = create(store)
    notify = AsyncMock()
    monkeypatch.setattr("medialink.worker.refresh_emby", notify)
    result = await drive(worker, task, "scanning", iterations=60)
    assert result["state"] == "scanning"
    assert {e["name"] for e in ol.fs[SEASON]} == {"中头奖还是要上班 S01E01.mkv", "中头奖还是要上班 S01E02.mkv",
                                                  "中头奖还是要上班 S01E02.chs.ass"}
    remaining = await worker.file_manifest(result["payload"]["staging"])
    assert {PurePosixPath(item["path"]).name for item in remaining} == {f"{AD}.MP4", "更多资源.url"}
    assert result["payload"]["scan_path"] == f"/115Strm/infuse/电视剧/{FOLDER}"
    assert result["payload"]["retained_count"] == 2
    assert all(len(names) == 1 for _, _, names in ol.library_moves) and len(ol.library_moves) == 3
    await worker.step(result)
    notify.assert_not_awaited()
    write_strm(store, result)
    assert (await drive(worker, result))["state"] == "completed"
    notify.assert_awaited_once()
    assert ol.offline_calls == 1


async def test_existing_series_folder_is_merged_and_same_name_is_never_overwritten(store):
    worker, task, ol = create(store)
    await ol.mkdir(SEASON)
    ol.fs[SEASON].append({"name": "中头奖还是要上班 S01E05.mkv", "size": 5, "id": "old", "is_dir": False})
    result = await drive(worker, task, "scan_queued", iterations=60)
    assert result["state"] == "scan_queued" and len(ol.fs[SEASON]) == 4
    worker, task, ol = create(Store(store.path.with_name("other.db")))
    await ol.mkdir(SEASON)
    ol.fs[SEASON].append({"name": "中头奖还是要上班 S01E01.mkv", "size": 5, "id": "old", "is_dir": False})
    task = await drive(worker, task, "placing", iterations=60)
    with pytest.raises(Problem, match="同名"):
        for _ in range(5):
            await worker.step(worker.store.task(task["id"]))
    assert next(e for e in ol.fs[SEASON] if e["name"] == "中头奖还是要上班 S01E01.mkv")["id"] == "old"
    assert not any("S01E01" in names[0] for _, _, names in ol.library_moves)


async def test_uncertain_library_move_reconciles_after_restart_without_repeating(store):
    worker, task, ol = create(store)
    ol.timeout_library = True
    task = await drive(worker, task, "placing", iterations=60)
    for _ in range(12):
        latest = Store(store.path).task(task["id"])
        if latest["state"] != "placing":
            break
        # A restarted worker sees only persisted evidence of the attempted move.
        restarted = Worker(Store(store.path), worker.vault, ol, worker.p115)
        latest["payload"]["next_poll"] = 0
        await restarted.step(latest)
    assert Store(store.path).task(task["id"])["state"] == "scan_queued"
    assert len(ol.library_moves) == 3


async def test_validated_model_suggestions_continue_without_manual_confirmation(store):
    store.configure({"llm_enabled": True})
    tree = [{"path": f"Release/Show {name}.mkv", "size": GB} for name in ("A", "B")]
    llm = SimpleNamespace(suggest=AsyncMock(side_effect=lambda naming, candidates: {
        item["id"]: {"season_number": 1, "episode_number": index + 1, "reason": "顺序"} for index, item in enumerate(candidates)}))
    worker, task, ol = create(store, {**TV, "seasons": {"1": 2, "2": 8}}, tree, llm)
    task = await drive(worker, task, "naming_analyzing", iterations=40)
    await worker.naming_jobs[task["id"]]
    latest = store.task(task["id"])
    assert latest["state"] == "renaming" and not latest["payload"]["naming"]["review_required"]
    assert (await drive(worker, latest, "scan_queued", iterations=40))["state"] == "scan_queued"
    assert {e["name"] for e in ol.fs[SEASON]} == {"中头奖还是要上班 S01E01.mkv", "中头奖还是要上班 S01E02.mkv"}


async def test_model_suggestion_outside_tmdb_range_pauses(store):
    store.configure({"llm_enabled": True})
    tree = [{"path": "Release/Show A.mkv", "size": GB}]
    llm = SimpleNamespace(suggest=AsyncMock(side_effect=lambda naming, candidates: {
        candidates[0]["id"]: {"season_number": 1, "episode_number": 9}}))
    worker, task, ol = create(store, {**TV, "seasons": {"1": 2}}, tree, llm)
    task = await drive(worker, task, "naming_analyzing", iterations=40)
    await worker.naming_jobs[task["id"]]
    latest = store.task(task["id"])
    assert latest["state"] == "needs_attention" and "只有 2 集" in latest["error"]
    assert not ol.library_moves


async def test_pre_download_selection_only_marks_files_to_keep(store):
    preview = preview_snapshot("a" * 40, JACKPOT, RELEASE)
    chosen = [item for item in preview["files"] if "S01E01" in item["path"]]
    selection = {"preview_id": preview["preview_id"], "root": preview["root"], "files": chosen,
                 "selected_ids": [item["id"] for item in chosen], "total_count": len(RELEASE)}
    assert [item["keep"] for item in preview["files"]] == [True, True, True, False, False]
    worker, task, ol = create(store, magnet_selection=selection)
    result = await drive(worker, task, "scan_queued", iterations=60)
    assert result["state"] == "scan_queued"
    assert {e["name"] for e in ol.fs[SEASON]} == {"中头奖还是要上班 S01E01.mkv"}
    reasons = {item["reason"] for item in result["payload"]["naming"]["excluded"]}
    assert "下载前未勾选" in reasons


def test_api_creates_library_tasks_layout_and_toggles_files(client, store):
    from test_naming import login

    login(client)
    client.app.state.tmdb.detail = AsyncMock(return_value={**TV, "seasons": [{"number": 1, "episodes": 10}]})
    assert client.get("/api/v1/naming/layout?media_type=tv&media_id=99").json() == {"folder": FOLDER}
    response = client.post("/api/v1/tasks", json={"link": MAGNET, "target": TARGET, "auto_naming": True,
                                                  "media": {"type": "tv", "id": 99}})
    task = store.task(response.json()["id"])
    assert task["payload"]["pipeline"] == 2 and task["payload"]["naming"]["version"] == 2
    assert task["payload"]["naming"]["media"]["seasons"] == {"1": 10}
    files = [{"path": item["path"], "size": item["size"]} for item in RELEASE]
    naming = {**task["payload"]["naming"], "files": files}
    store.update_task(task["id"], "needs_attention", naming=naming, resume_state="naming_planning")
    ad = file_id(f"{JACKPOT}/{AD}.MP4")
    view = client.patch(f"/api/v1/tasks/{task['id']}/naming", json={"files": [{"id": ad, "included": True}]}).json()
    assert any(item["id"] == ad for item in view["files"]) and view["excluded_count"] == 1
    other = file_id(f"{JACKPOT}/更多资源.url")
    assert client.patch(f"/api/v1/tasks/{task['id']}/naming", json={"files": [{"id": other, "included": True}]}).status_code == 422
    plain = client.post("/api/v1/tasks", json={"link": "magnet:?xt=urn:btih:" + "c" * 40, "target": TARGET})
    assert store.task(plain.json()["id"])["payload"]["pipeline"] == 1


async def test_conflict_before_any_move_allows_another_category(store):
    from medialink.app import public_task

    worker, task, ol = create(store)
    await ol.mkdir(SEASON)
    ol.fs[SEASON].append({"name": "中头奖还是要上班 S01E01.mkv", "size": 5, "id": "old", "is_dir": False})
    task = await drive(worker, task, "placing", iterations=60)
    with pytest.raises(Problem, match="同名"):
        for _ in range(5):
            await worker.step(store.task(task["id"]))
    store.update_task(task["id"], "needs_attention", resume_state="placing")
    assert public_task(store.task(task["id"]))["can_change_target"]
    changed = await worker.retry(task["id"], "/115/infuse/日韩电视剧")
    assert changed["state"] == "placing"
    assert (await drive(worker, changed, "scan_queued", iterations=30))["state"] == "scan_queued"
    assert len(ol.fs[f"/115/infuse/日韩电视剧/{FOLDER}/Season 01"]) == 3
