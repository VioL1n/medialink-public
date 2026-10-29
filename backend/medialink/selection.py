"""Persisted, reconciled extraction of selected files from a complete cloud download."""
import time
from pathlib import PurePosixPath

from .clients import RemoteFailure
from .core import Problem

RECONCILE_SECONDS = 180
UNCERTAIN_MESSAGE = "选中文件移动结果无法确认，请检查两处暂存目录；不会重复移动或覆盖"
PLACEMENT_UNCERTAIN = "入库移动结果无法确认，请检查暂存目录和媒体库；不会重复移动或覆盖"


def single_file_plan(plan):
    """Split legacy batches without discarding their persisted operation evidence."""
    return [{**group, "files": [item]} for group in plan for item in group["files"]]


def map_selection(selection, files):
    actual = {item["path"]: item for item in files}
    wanted = selection["files"]
    candidates = []
    for strip_root in (False, True):
        root = selection.get("root")
        if strip_root and not root:
            continue
        paths = [item["path"][len(root) + 1:] if strip_root else item["path"] for item in wanted]
        if len(set(paths)) != len(paths):
            continue
        if all(path in actual and actual[path]["size"] == item["size"] for path, item in zip(paths, wanted, strict=True)):
            if paths not in candidates:
                candidates.append(paths)
    if len(candidates) != 1:
        raise Problem("选中文件无法与下载内容唯一匹配，请核对路径和大小；文件已保留", 409)
    selected = [actual[path] for path in candidates[0]]
    groups = {}
    for item in selected:
        groups.setdefault(str(PurePosixPath(item["path"]).parent), []).append(item)
    return single_file_plan([
        {"directory": directory, "files": entries, "status": "pending"} for directory, entries in sorted(groups.items())])


def matches(entry, original):
    return bool(entry and not entry.get("is_dir") and int(entry.get("size") or 0) == original["size"]
                and all(str(entry.get(key)) == value for key, value in original.get("identity", {}).items()))


async def retry_selection_plan(worker, task):
    """Only an explicit retry may resubmit an unchanged, expired uncertain move."""
    payload = task["payload"]
    plan = single_file_plan(payload["selection_plan"])
    if not payload.get("selection_initialized"):
        return plan
    directories = {}
    for group in plan:
        if group["status"] == "done":
            continue
        directory = group["directory"]
        if directory not in directories:
            source = str(PurePosixPath(payload["download_staging"]) / directory)
            target = str(PurePosixPath(payload["selection_staging"]) / directory)
            directories[directory] = (
                {entry["name"]: entry for entry in await worker.openlist.entries(source)},
                {entry["name"]: entry for entry in await worker.openlist.entries(target)})
        src, dst = directories[directory]
        item = group["files"][0]
        name = PurePosixPath(item["path"]).name
        if group["status"] == "attempted":
            if name not in src and matches(dst.get(name), item) and (group.get("acknowledged") or item.get("identity")):
                group["status"] = "done"
                continue
            if (not item.get("identity") or not matches(src.get(name), item) or name in dst):
                raise Problem(UNCERTAIN_MESSAGE, 409)
            if time.time() - group["attempted_at"] < RECONCILE_SECONDS:
                raise Problem("选中文件移动仍在核对中，请稍后重试；不会重复提交", 409)
            group["status"] = "pending"
            group.pop("attempted_at", None)
            group.pop("acknowledged", None)
        elif not matches(src.get(name), item) or name in dst:
            raise Problem("暂存文件已变化或选中目录存在同名内容，请核对；不会覆盖", 409)
    return plan


async def select_step(worker, task):
    payload = task["payload"]
    original_root, selected_root = payload["download_staging"], payload["selection_staging"]
    plan = payload["selection_plan"]
    if any(len(group["files"]) > 1 for group in plan):
        plan = single_file_plan(plan)
        worker.update(task, selection_plan=plan)
    if not payload.get("selection_initialized"):
        await worker.openlist.mkdir(selected_root)
        if await worker.file_manifest(selected_root):
            raise Problem("选中文件暂存目录已有内容，请核对；不会覆盖", 409)
        for relative in sorted({group["directory"] for group in plan}):
            directory = PurePosixPath(relative)
            for index in range(1, len(directory.parts) + 1):
                await worker.openlist.mkdir(str(PurePosixPath(selected_root).joinpath(*directory.parts[:index])))
        worker.update(task, selection_initialized=True)
        return
    for group in plan:
        if group["status"] == "done":
            continue
        source = str(PurePosixPath(original_root) / group["directory"])
        target = str(PurePosixPath(selected_root) / group["directory"])
        src = {entry["name"]: entry for entry in await worker.openlist.entries(source)}
        dst = {entry["name"]: entry for entry in await worker.openlist.entries(target)}
        items = group["files"]
        names = [PurePosixPath(item["path"]).name for item in items]
        if group["status"] == "attempted":
            finished = all(name not in src and matches(dst.get(name), item)
                           and (group.get("acknowledged") or item.get("identity"))
                           for name, item in zip(names, items, strict=True))
            if finished:
                group["status"] = "done"
                worker.update(task, selection_plan=plan, next_poll=0)
                return
            untouched = all(matches(src.get(name), item) and name not in dst
                            for name, item in zip(names, items, strict=True))
            if untouched and time.time() - group["attempted_at"] < RECONCILE_SECONDS:
                worker.update(task, next_poll=time.time() + worker.store.settings()["poll_seconds"])
                return
            raise Problem(UNCERTAIN_MESSAGE, 409)
        if not all(matches(src.get(name), item) and name not in dst for name, item in zip(names, items, strict=True)):
            raise Problem("暂存文件已变化或选中目录存在同名内容，请核对；不会覆盖", 409)
        group.update(status="attempted", attempted_at=time.time())
        worker.update(task, selection_plan=plan)
        try:
            await worker.openlist.move(source, target, names)
        except RemoteFailure as exc:
            if not exc.uncertain:
                group["status"] = "pending"
                worker.update(task, selection_plan=plan)
                raise
            worker.update(task, selection_plan=plan, next_poll=time.time() + worker.store.settings()["poll_seconds"])
            return
        group["acknowledged"] = True
        worker.update(task, selection_plan=plan)
        return
    selected = {item["path"]: item for group in plan for item in group["files"]}
    current = {item["path"]: item for item in await worker.file_manifest(selected_root)}
    if current != selected:
        raise Problem("选中文件清单发生变化，请核对；暂未入库", 409)
    remaining = {item["path"]: item for item in await worker.file_manifest(original_root)}
    expected = {item["path"]: item for item in payload["download_files"] if item["path"] not in selected}
    if remaining != expected:
        raise Problem("未选中的暂存文件清单发生变化，请核对；暂未入库", 409)
    worker.update(task, "selected_ready", staging=selected_root, retained_count=len(remaining), next_poll=0)


def placement_plan(plan, files):
    """One move per kept file, from its renamed staging path into the title folder."""
    actual = {item["path"]: item for item in files}
    return [{"source": item["target"], "destination": item["destination"], "kind": item["kind"],
             "size": actual[item["target"]]["size"], "identity": actual[item["target"]].get("identity", {}),
             "status": "pending"} for item in plan]


def placement_paths(staging, target, item):
    return PurePosixPath(staging) / item["source"], PurePosixPath(target) / item["destination"]


async def retry_placement_plan(worker, task):
    """Only an explicit retry may resubmit an unchanged, expired uncertain library move."""
    payload = task["payload"]
    plan = payload["placement"]
    for item in plan:
        if item["status"] != "attempted":
            continue
        source, destination = placement_paths(payload["staging"], task["target"], item)
        src = {entry["name"]: entry for entry in await worker.openlist.entries(str(source.parent))}
        dst = {entry["name"]: entry for entry in await worker.openlist.entries(str(destination.parent))}
        if source.name not in src and matches(dst.get(destination.name), item) and (
                item.get("acknowledged") or item.get("identity")):
            item["status"] = "done"
            continue
        if not item.get("identity") or not matches(src.get(source.name), item) or destination.name in dst:
            raise Problem(PLACEMENT_UNCERTAIN, 409)
        if time.time() - item["attempted_at"] < RECONCILE_SECONDS:
            raise Problem("入库移动仍在核对中，请稍后重试；不会重复提交", 409)
        item["status"] = "pending"
        item.pop("attempted_at", None)
        item.pop("acknowledged", None)
    return plan


async def place_step(worker, task):
    """Move curated, renamed files into target/<title folder>/[Season NN], merging existing folders."""
    payload = task["payload"]
    staging, target = payload["staging"], task["target"]
    plan = payload["placement"]
    poll = worker.store.settings()["poll_seconds"]
    if not payload.get("placement_initialized"):
        for relative in sorted({str(PurePosixPath(item["destination"]).parent) for item in plan}):
            directory = PurePosixPath(relative)
            for index in range(1, len(directory.parts) + 1):
                await worker.openlist.mkdir(str(PurePosixPath(target).joinpath(*directory.parts[:index])))
        worker.update(task, placement_initialized=True, next_poll=0)
        return
    for item in plan:
        if item["status"] == "done":
            continue
        source, destination = placement_paths(staging, target, item)
        src = {entry["name"]: entry for entry in await worker.openlist.entries(str(source.parent))}
        dst = {entry["name"]: entry for entry in await worker.openlist.entries(str(destination.parent))}
        if item["status"] == "attempted":
            if source.name not in src and matches(dst.get(destination.name), item) and (
                    item.get("acknowledged") or item.get("identity")):
                item["status"] = "done"
                worker.update(task, placement=plan, next_poll=0)
                return
            untouched = matches(src.get(source.name), item) and destination.name not in dst
            if untouched and time.time() - item["attempted_at"] < RECONCILE_SECONDS:
                worker.update(task, next_poll=time.time() + poll)
                return
            raise Problem(PLACEMENT_UNCERTAIN, 409)
        if destination.name in dst:
            raise Problem("媒体库已有同名文件，不会覆盖；请核对后重试或更换入库目录", 409)
        if not matches(src.get(source.name), item):
            raise Problem("暂存文件已变化，请核对；暂未入库", 409)
        item.update(status="attempted", attempted_at=time.time())
        worker.update(task, placement=plan)
        try:
            await worker.openlist.move(str(source.parent), str(destination.parent), [source.name])
        except RemoteFailure as exc:
            if not exc.uncertain:
                item["status"] = "pending"
                item.pop("attempted_at", None)
                worker.update(task, placement=plan)
                raise
            worker.update(task, placement=plan, next_poll=time.time() + poll)
            return
        item["acknowledged"] = True
        worker.update(task, placement=plan)
        return
    for item in plan:
        _, destination = placement_paths(staging, target, item)
        entries = {entry["name"]: entry for entry in await worker.openlist.entries(str(destination.parent))}
        if not matches(entries.get(destination.name), item):
            raise Problem("媒体库中的入库文件核对不一致，请检查；不会自动覆盖", 409)
    settings = worker.store.settings()
    folder = payload["naming"]["folder"]
    relative = PurePosixPath(target).relative_to(settings["media_root"]) / folder
    worker.update(task, "scan_queued", scan_path=str(PurePosixPath(settings["strm_root"]) / relative),
                  videos=[item["destination"] for item in plan if item["kind"] == "video"],
                  retained_count=len(payload["naming"]["files"]) - len(plan), next_poll=0)
