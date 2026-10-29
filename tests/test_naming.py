import json
import time
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock

import pytest
from medialink.clients import RemoteFailure
from medialink.core import Problem, Store
from medialink.naming import build_plan, episode_info, file_id, normalize_resolution, safe_title
from test_medialink import MAGNET, drive, new_worker

pytest_plugins = ["test_medialink"]

MOVIE = {"id": 157336, "type": "movie", "title": "星际穿越", "chinese_title": "星际穿越", "original_title": "Interstellar"}
TV = {"id": 1396, "type": "tv", "title": "绝命毒师", "chinese_title": "绝命毒师", "original_title": "Breaking Bad"}


def plan(paths, media=MOVIE, **extra):
    return build_plan({"version": 1, "media": media, "files": [{"path": p, "size": 1234} for p in paths], **extra})


@pytest.mark.parametrize("source,target", [("4k", "2160P"), ("8K", "4320P"), ("1080i", "1080I"), ("720p", "720P")])
def test_resolution_normalization(source, target):
    assert normalize_resolution(source) == target


@pytest.mark.parametrize("path,season,episode", [
    ("Show.S01E02.1080p.mkv", 1, 2), ("Show 1x03.mkv", 1, 3),
    ("第一季/第十二集.mp4", 1, 12), ("Season 12/Show.E105.mkv", 12, 105),
    ("Specials/Show.S00E01.mkv", 0, 1), ("S01/Show.S01E01-1080P.mkv", 1, 1),
])
def test_clear_episode_formats(path, season, episode):
    assert episode_info(path) == (season, episode, None, False)


@pytest.mark.parametrize("path", ["S01E01E02.mkv", "S01E01-E02.mkv", "S01E01-02.mkv", "1x01-02.mkv", "第一季/第1至2集.mkv", "S01E01.S01E02.mkv"])
def test_merged_episodes_cannot_be_overridden(path):
    result = plan([path], TV, overrides={file_id(path): {"season_number": 1, "episode_number": 1}})
    assert result["errors"] and result["plan"][0]["target"] is None
    assert "多集" in result["errors"][0]


def test_movie_original_title_quality_priority_and_attachments():
    source = "Old.Release.2014.1080p.DTS.mkv"
    result = plan([source, "Old.Release.2014.1080p.DTS.zh-CN.forced.srt", "poster.jpg", "unrelated.srt"], resolution="4K")
    assert not result["errors"]
    assert [p["target"] for p in result["plan"]] == ["Interstellar 1080P.mkv", "Interstellar 1080P.zh-CN.forced.srt"]
    assert result["warnings"] == ["unrelated.srt：字幕对应关系不明确，保留原名"]
    assert result["plan"][0]["basis"] == "文件名"
    assert plan([source], overrides={file_id(source): {"resolution": "8K"}})["plan"][0]["target"] == "Interstellar 4320P.mkv"
    assert plan(["4K/1080p/movie.mkv"])["plan"][0]["target"] == "4K/1080p/Interstellar 1080P.mkv"
    assert plan(["movie.mkv"], resolution="1080I")["plan"][0]["target"] == "Interstellar 1080I.mkv"


def test_tv_chinese_title_original_fallback_extension_and_season_selection():
    assert plan(["Season 01/Show.1x02.2160p.MKV"], TV)["plan"][0]["target"] == "Season 01/绝命毒师 S01E02.MKV"
    assert plan(["Show.E102.mp4"], {**TV, "chinese_title": None}, season_number=12)["plan"][0]["target"] == "Breaking Bad S12E102.mp4"
    assert plan(["Specials/Show.E01.mkv"], TV)["plan"][0]["target"] == "Specials/绝命毒师 S00E01.mkv"


@pytest.mark.parametrize("paths,media,extra", [
    (["movie.mkv"], None, {}), (["movie.mkv"], MOVIE, {}),
    (["show.mkv"], TV, {}), (["S02/Show.S01E01.mkv"], TV, {}),
    (["S01E01.mkv"], TV, {"season_number": 2}),
    (["Movie.1080p.2160p.mkv"], MOVIE, {}),
    (["a.1080p.mkv", "b.1080p.mkv"], MOVIE, {}),
    (["movie.1080p.mkv", "Interstellar 1080P.mkv"], MOVIE, {}),
    (["movie.1080p.mkv"], MOVIE, {"directories": ["Interstellar 1080P.mkv"]}),
    (["movie.1080p.mkv"], {**MOVIE, "original_title": "a" * 260}, {}),
])
def test_incomplete_or_conflicting_plan_never_passes(paths, media, extra):
    assert plan(paths, media, **extra)["errors"]


def test_safe_title_and_ambiguous_subtitle():
    assert safe_title(' A/B:C\\D?*"<>|\x00\x85\u202e. ') == "A B C D"
    assert plan(["movie.1080p.mkv"], {**MOVIE, "original_title": 'A/B:C'})["plan"][0]["target"] == "A B C 1080P.mkv"
    result = plan(["show.S01E01.mkv", "show.S01E01.DTS.srt", "other.srt"], TV)
    assert result["warnings"]  # Unmatched subtitles remain unchanged.


def add_naming(worker, task, media=MOVIE, resolution="1080P"):
    return worker.update(task, naming={"version": 1, "media": media, "resolution": resolution,
                                      "files": [], "plan": [], "overrides": {}, "locked": False})


def enable_rename(ol, timeout=False):
    calls = []

    async def rename(path, name):
        calls.append((path, name))
        p = PurePosixPath(path)
        entries = ol.fs[str(p.parent)]
        if any(e["name"] == name for e in entries):
            raise Problem("collision", 409)
        entry = next(e for e in entries if e["name"] == p.name)
        entry["name"] = name
        if timeout:
            raise RemoteFailure("timeout", uncertain=True)

    ol.rename = rename
    return calls


@pytest.mark.parametrize("kind", ["magnet", "share"])
async def test_full_new_ingest_only_checks_renamed_strm(store, monkeypatch, kind):
    worker, task, ol, p115 = new_worker(store, kind)
    task = add_naming(worker, task)
    calls = enable_rename(ol)
    notify = AsyncMock()
    monkeypatch.setattr("medialink.worker.refresh_emby", notify)
    result = await drive(worker, task, "scanning", iterations=60)
    assert result["state"] == "scanning"
    assert calls == [(result["payload"]["staging"] + "/open-movie.mkv", "Interstellar 1080P.mkv")]
    assert ol.fs["/115/infuse"][0]["name"] == "Interstellar 1080P.mkv"
    root = Path(store.settings()["strm_local"]) / "infuse"
    root.mkdir(parents=True)
    (root / "open-movie.strm").write_text("old")
    await worker.step(result)
    notify.assert_not_awaited()
    assert store.task(task["id"])["state"] == "scanning"
    (root / "Interstellar 1080P.strm").write_text("renamed")
    assert (await drive(worker, task))["state"] == "completed"
    notify.assert_awaited_once()
    assert ol.offline_calls + p115.calls == 1


async def test_missing_metadata_and_conflicts_do_not_move_or_rename(store):
    worker, task, ol, _ = new_worker(store)
    task = add_naming(worker, task, None, None)
    calls = enable_rename(ol)
    task = await drive(worker, task, "naming_planning")
    with pytest.raises(Problem, match="TMDB"):
        await worker.step(task)
    assert not calls and not ol.move_calls
    assert store.task(task["id"])["payload"]["naming"]["plan"][0]["id"] == file_id("open-movie.mkv")


async def test_partial_rename_timeout_and_restart_keeps_plan(store):
    worker, task, ol, _ = new_worker(store)
    task = add_naming(worker, task)
    task = await drive(worker, task, "naming_planning")
    staging = task["payload"]["staging"]
    ol.fs[staging][0]["id"] = "video-1"
    ol.fs[staging].append({"id": "sub-1", "name": "open-movie.zh.srt", "size": 30})
    worker.update(task, naming={**task["payload"]["naming"], "files": await worker.file_manifest(staging)})
    calls = enable_rename(ol, timeout=True)
    await worker.step(store.task(task["id"]))
    await worker.step(store.task(task["id"]))
    assert len(calls) == 1 and not ol.move_calls
    assert store.task(task["id"])["payload"]["naming"]["locked"]
    worker.store = Store(store.path)
    task = await drive(worker, task, "organizing", iterations=30)
    assert task["state"] == "organizing" and len(calls) == 2
    assert task["payload"]["videos"] == ["Interstellar 1080P.mkv"]
    assert {e["name"] for e in ol.fs[staging]} == {"Interstellar 1080P.mkv", "Interstellar 1080P.zh.srt"}
    assert all(item["status"] == "done" for item in task["payload"]["naming"]["plan"])


async def test_uncertain_rename_without_identity_cannot_claim_same_size_file(store):
    worker, task, ol, _ = new_worker(store)
    task = add_naming(worker, task)
    task = await drive(worker, task, "renaming")
    calls = enable_rename(ol, timeout=True)
    await worker.step(task)
    with pytest.raises(Problem, match="无法确认"):
        await worker.step(store.task(task["id"]))
    assert len(calls) == 1 and ol.move_calls == 0


async def test_uncertain_rename_with_source_present_never_repeats_write(store):
    worker, task, ol, _ = new_worker(store)
    task = add_naming(worker, task)
    task = await drive(worker, task, "renaming")
    ol.rename = AsyncMock(side_effect=RemoteFailure("timeout", uncertain=True))
    await worker.step(task)
    task = store.task(task["id"])
    task["payload"]["naming"]["plan"][0]["attempted_at"] = time.time() - 181
    worker.update(task, naming=task["payload"]["naming"])
    with pytest.raises(Problem, match="原文件仍在"):
        await worker.step(store.task(task["id"]))
    assert ol.rename.await_count == 1 and ol.move_calls == 0


async def test_preflight_and_final_tree_changes_pause(store):
    worker, task, ol, _ = new_worker(store)
    task = add_naming(worker, task)
    task = await drive(worker, task, "naming_planning")
    staging = task["payload"]["staging"]
    ol.fs[staging].append({"name": "new.txt", "size": 1})
    calls = enable_rename(ol)
    with pytest.raises(Problem, match="原始清单"):
        await worker.step(task)
    assert not calls
    ol.fs[staging].pop()
    await worker.step(task)
    await worker.step(store.task(task["id"]))
    await worker.step(store.task(task["id"]))
    ol.fs[staging].append({"name": "new.txt", "size": 1})
    with pytest.raises(Problem, match="文件清单"):
        await worker.step(store.task(task["id"]))
    assert ol.move_calls == 0


def login(client):
    assert client.post("/api/v1/auth/setup", json={"password": "naming-test-password"}).status_code == 200


def test_task_api_snapshot_and_duplicate_legacy_compatibility(client, store):
    login(client)
    client.app.state.tmdb.detail = AsyncMock(return_value=MOVIE)
    response = client.post("/api/v1/tasks", json={"link": MAGNET, "target": "/115/infuse", "auto_naming": True, "media": {"type": "movie", "id": 157336, "title": "evil"}, "resolution": "4K"})
    assert response.status_code == 200
    task = response.json()
    assert task["naming"]["media"]["original_title"] == "Interstellar"
    assert task["naming"]["resolution"] == "2160P"
    client.app.state.tmdb.detail.reset_mock()
    assert client.post("/api/v1/tasks", json={"link": MAGNET, "target": "/115/infuse"}).json()["id"] == task["id"]
    client.app.state.tmdb.detail.assert_not_awaited()
    assert client.get(f"/api/v1/tasks/{task['id']}/naming").json()["editable"]
    legacy = store.create_task("magnet:" + "b" * 40, "/115/infuse", {"kind": "magnet"})
    assert client.get(f"/api/v1/tasks/{legacy['id']}/naming").status_code == 409
    assert client.patch(f"/api/v1/tasks/{legacy['id']}/naming", json={}).status_code == 409


@pytest.mark.parametrize("option", [{}, {"auto_naming": False}])
@pytest.mark.parametrize("kind", ["magnet", "share"])
async def test_task_api_without_auto_naming_preserves_files(client, store, monkeypatch, option, kind):
    from test_medialink import FakeOpenList, FakeP115

    login(client)
    worker = client.app.state.worker
    ol = FakeOpenList()
    p115 = FakeP115(ol)
    worker.openlist, worker.p115 = ol, p115
    client.app.state.p115.share_entries = AsyncMock(side_effect=p115.share_entries)
    client.app.state.tmdb.detail = AsyncMock(side_effect=AssertionError("TMDB must not be required"))
    link = MAGNET if kind == "magnet" else "https://115.com/s/testshare"
    response = client.post("/api/v1/tasks", json={"link": link, "target": "/115/infuse", **option,
                           "media": {"type": "movie", "id": 157336}, "resolution": "HD"})
    assert response.status_code == 200
    task = response.json()
    assert task["naming"] is None
    client.app.state.tmdb.detail.assert_not_awaited()
    assert Store(store.path).task(task["id"])["payload"]["naming"] is None
    assert client.get(f"/api/v1/tasks/{task['id']}/naming").status_code == 409
    assert client.patch(f"/api/v1/tasks/{task['id']}/naming", json={}).status_code == 409
    calls = enable_rename(ol)
    notify = AsyncMock()
    monkeypatch.setattr("medialink.worker.refresh_emby", notify)
    result = await drive(worker, task, "scanning")
    assert result["state"] == "scanning"
    assert not calls
    assert ol.fs["/115/infuse"][0]["name"] == "open-movie.mkv"
    await worker.step(result)
    notify.assert_not_awaited()
    root = Path(store.settings()["strm_local"]) / "infuse"
    root.mkdir(parents=True)
    (root / "open-movie.strm").write_text("original")
    assert (await drive(worker, task))["state"] == "completed"
    notify.assert_awaited_once()
    assert ol.offline_calls + p115.calls == 1


def test_naming_api_paused_task_corrections_validation_and_lock(client, store):
    login(client)
    task = client.post("/api/v1/tasks", json={"link": MAGNET, "target": "/115/infuse"}).json()
    naming = plan(["show.mkv"], None)
    store.update_task(task["id"], "needs_attention", naming=naming, resume_state="naming_planning")
    client.app.state.tmdb.detail = AsyncMock(return_value=TV)
    url = f"/api/v1/tasks/{task['id']}/naming"
    response = client.patch(url, json={"media": {"type": "tv", "id": 1396}, "files": [{"id": file_id("show.mkv"), "season_number": 0, "episode_number": 1}]})
    assert response.status_code == 200
    assert response.json()["files"][0]["target"] == "绝命毒师 S00E01.mkv"
    assert store.task(task["id"])["state"] == "naming_planning"
    assert "identity" not in json.dumps(response.json())
    for body in [{"files": [{"id": "f" * 24, "episode_number": 1}]}, {"files": [{"id": file_id("show.mkv"), "target": "../evil"}]}, {"target": "../evil"}, {"resolution": "HD"}, {"files": [{"id": file_id("show.mkv"), "episode_number": 0}]}]:
        assert client.patch(url, json=body).status_code == 422
    latest = store.task(task["id"])["payload"]["naming"]
    store.update_task(task["id"], "renaming", naming={**latest, "locked": True})
    assert client.patch(url, json={"resolution": "1080P"}).status_code == 409
    assert not client.get(url).json()["editable"]


async def test_nested_episode_pack_keeps_directories_and_unrelated_attachments(store):
    worker, task, ol, _ = new_worker(store)
    task = add_naming(worker, task, TV, None)
    task = await drive(worker, task, "transferring")
    staging = task["payload"]["staging"]
    ol.fs[staging] = [{"name": "Season 01", "is_dir": True}, {"name": "Empty", "is_dir": True}]
    ol.fs[staging + "/Season 01"] = [
        {"name": "Release.S01E01.2160p.mkv", "size": 1234, "id": "v1"},
        {"name": "Release.S01E01.2160p.chs.forced.srt", "size": 12, "id": "s1"},
        {"name": "Release.S01E02.2160p.mkv", "size": 1234, "id": "v2"},
        {"name": "poster.jpg", "size": 100, "id": "poster"},
        {"name": "unmatched.srt", "size": 10, "id": "unmatched"},
    ]
    ol.fs[staging + "/Empty"] = []
    calls = enable_rename(ol)
    task = await drive(worker, task, "organizing", iterations=60)
    assert task["state"] == "organizing" and len(calls) == 3
    assert set(task["payload"]["videos"]) == {"Season 01/绝命毒师 S01E01.mkv", "Season 01/绝命毒师 S01E02.mkv"}
    assert {e["name"] for e in ol.fs[staging + "/Season 01"]} == {"绝命毒师 S01E01.mkv", "绝命毒师 S01E01.chs.forced.srt", "绝命毒师 S01E02.mkv", "poster.jpg", "unmatched.srt"}
    assert {e["name"] for e in task["payload"]["manifest"]} == {"Season 01", "Empty"}
    assert task["payload"]["naming"]["warnings"]


async def test_destination_conflict_after_rename_preserves_new_files_and_retries_target(store):
    worker, task, ol, _ = new_worker(store)
    task = add_naming(worker, task)
    calls = enable_rename(ol)
    task = await drive(worker, task, "organizing")
    ol.fs["/115/infuse"].append({"name": "Interstellar 1080P.mkv", "size": 100})
    with pytest.raises(Problem, match="同名"):
        await worker.step(task)
    assert ol.move_calls == 0 and len(calls) == 1
    assert ol.fs[task["payload"]["staging"]][0]["name"] == "Interstellar 1080P.mkv"
    store.update_task(task["id"], "needs_attention", resume_state="organizing")
    changed = await worker.retry(task["id"], "/115/infuse/new")
    result = await drive(worker, changed, "scan_queued")
    assert result["state"] == "scan_queued" and len(calls) == 1
    assert ol.fs["/115/infuse/new"][0]["name"] == "Interstellar 1080P.mkv"


async def test_uncertain_rename_identity_mismatch_never_moves(store):
    worker, task, ol, _ = new_worker(store)
    task = add_naming(worker, task)
    task = await drive(worker, task, "naming_planning")
    staging = task["payload"]["staging"]
    ol.fs[staging][0]["id"] = "original"
    worker.update(task, naming={**task["payload"]["naming"], "files": await worker.file_manifest(staging)})
    calls = enable_rename(ol, timeout=True)
    await worker.step(store.task(task["id"]))
    await worker.step(store.task(task["id"]))
    ol.fs[staging][0]["id"] = "unrelated-with-same-size"
    with pytest.raises(Problem, match="无法确认"):
        await worker.step(store.task(task["id"]))
    assert len(calls) == 1 and ol.move_calls == 0


def test_correction_clear_override_and_terminal_task_readonly(client, store):
    login(client)
    task = client.post("/api/v1/tasks", json={"link": MAGNET, "target": "/115/infuse"}).json()
    source = "movie.1080p.mkv"
    naming = plan([source], overrides={file_id(source): {"resolution": "2160P"}})
    store.update_task(task["id"], "needs_attention", naming=naming, resume_state="naming_planning")
    url = f"/api/v1/tasks/{task['id']}/naming"
    response = client.patch(url, json={"files": [{"id": file_id(source), "resolution": None}]})
    assert response.json()["files"][0]["target"] == "Interstellar 1080P.mkv"
    assert store.task(task["id"])["payload"]["naming"]["overrides"][file_id(source)] == {}
    for state in ("canceled", "completed"):
        store.update_task(task["id"], state)
        assert not client.get(url).json()["editable"]
        assert client.patch(url, json={}).status_code == 409


def test_task_list_omits_large_file_plans_but_detail_has_preview(client, store):
    login(client)
    naming = plan(["movie.1080p.mkv"])
    task = store.create_task("magnet:" + "c" * 40, "/115/infuse", {"kind": "magnet", "naming": naming})
    assert client.get("/api/v1/tasks").json()[0]["naming"]["files"] == []
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["naming"]["files"][0]["target"] == "Interstellar 1080P.mkv"
