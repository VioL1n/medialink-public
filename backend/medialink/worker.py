import asyncio
import json
import time
import uuid
from pathlib import Path, PurePosixPath

from .clients import RemoteFailure, refresh_emby
from .core import Problem, blocking
from .llm import BATCH_SIZE, NamingLLM
from .naming import VIDEO_EXTENSIONS, bind_confirmed_preview, build_plan, model_candidates
from .selection import (
    map_selection,
    place_step,
    placement_plan,
    retry_placement_plan,
    retry_selection_plan,
    select_step,
)


class Worker:
    def __init__(self, store, vault, openlist, p115, llm=None):
        self.store, self.vault, self.openlist, self.p115 = store, vault, openlist, p115
        self.lock = asyncio.Lock()
        self.stopped = False
        self.llm = llm or NamingLLM(store, vault)
        self.naming_jobs = {}

    async def close(self):
        jobs = list(self.naming_jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    def cancel_naming_job(self, task_id):
        job = self.naming_jobs.pop(task_id, None)
        if job:
            job.cancel()

    async def verify_naming_manifest(self, task):
        naming = task["payload"]["naming"]
        staging = task["payload"]["staging"]
        current = await self.file_manifest(staging)
        if ({f["path"]: f for f in current} != {f["path"]: f for f in naming["files"]}
                or set(self.manifest_directories) != set(naming.get("directories", []))):
            raise Problem("暂存内容与原始清单不符，请检查文件；命名尚未执行", 409)

    def pause_naming(self, task, naming):
        state = "needs_attention" if naming.get("errors") else "naming_review"
        return self.update(task, state, naming=naming, resume_state="naming_planning",
                           error="；".join(naming.get("errors", [])) or None,
                           failures=0, retry_at=0, next_poll=0)

    def start_naming_analysis(self, task):
        previous = task["payload"]["naming"]
        # A completed analysis can be explicitly regenerated. Failed batches keep their valid results.
        naming = build_plan({**previous, "suggestions": {} if previous.get("llm", {}).get("status") == "completed"
                             else previous.get("suggestions", {})})
        candidates = model_candidates(naming)
        if not self.store.settings()["llm_enabled"]:
            raise Problem("请先在服务连接中启用大模型辅助命名", 409)
        if not candidates:
            raise Problem("没有可由大模型补全的缺失信息，请检查 TMDB、冲突或人工修正", 409)
        self.cancel_naming_job(task["id"])
        if naming.get("preview"):
            naming["preview"]["confirmed"] = False
        run_id = uuid.uuid4().hex
        naming.update(review_required=True, revision=naming.get("revision", 0) + 1,
                      llm={"status": "running", "message": "正在识别缺失的命名信息", "run_id": run_id})
        task = self.update(task, "naming_analyzing", naming=naming, retry_at=0, failures=0)
        job = asyncio.create_task(self.analyze_naming(task, candidates, run_id))
        self.naming_jobs[task["id"]] = job

        def finished(done):
            if self.naming_jobs.get(task["id"]) is done:
                self.naming_jobs.pop(task["id"], None)

        job.add_done_callback(finished)
        return task

    def current_naming_run(self, task_id, run_id):
        task = self.store.task(task_id)
        llm = (task["payload"].get("naming") or {}).get("llm", {})
        return task if task["state"] == "naming_analyzing" and llm.get("run_id") == run_id else None

    async def analyze_naming(self, task, candidates, run_id):
        try:
            for offset in range(0, len(candidates), BATCH_SIZE):
                # No worker lock is held during credentials lookup or network I/O.
                result = await self.llm.suggest(task["payload"]["naming"], candidates[offset:offset + BATCH_SIZE])
                async with self.lock:
                    latest = self.current_naming_run(task["id"], run_id)
                    if latest is None:
                        return
                    naming = latest["payload"]["naming"]
                    naming["suggestions"] = naming.get("suggestions", {}) | result
                    naming["revision"] += 1
                    naming = build_plan(naming)
                    self.update(latest, naming=naming)
            async with self.lock:
                latest = self.current_naming_run(task["id"], run_id)
                if latest:
                    naming = build_plan(latest["payload"]["naming"])
                    if latest["payload"].get("pipeline") == 2 and not naming["errors"]:
                        # Library plans already passed curation and TMDB checks; no manual confirmation needed.
                        naming.update(review_required=False, confirmed_revision=naming["revision"],
                                      llm={"status": "completed", "message": "识别完成并通过 TMDB 校验，自动继续入库"})
                        self.update(latest, "renaming", naming=naming, resume_state="renaming", error=None,
                                    failures=0, retry_at=0, next_poll=0)
                        return
                    naming["llm"] = {"status": "completed", "message": "识别完成，请核对名称后确认重命名"}
                    self.pause_naming(latest, naming)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self.lock:
                latest = self.current_naming_run(task["id"], run_id)
                if latest:
                    message = exc.message if isinstance(exc, Problem) else "大模型识别失败，请人工补全或重新识别"
                    naming = latest["payload"]["naming"]
                    naming["llm"] = {"status": "failed", "message": message}
                    self.update(latest, "needs_attention", naming=naming, error=message,
                                resume_state="naming_planning", retry_at=0, failures=0)

    async def run(self):
        while not self.stopped:
            async with self.lock:
                for task in reversed(self.store.tasks(active=True)):
                    if task["payload"].get("retry_at", 0) > time.time():
                        continue
                    if task["state"] in {"downloading", "reconciling", "transferring", "moving", "scan_queued", "scanning"}:
                        if task["payload"].get("next_poll", 0) > time.time():
                            continue
                        task = self.update(task, next_poll=time.time()+self.store.settings()["poll_seconds"])
                    if task["state"] in {"renaming", "selecting_files", "placing"} and task["payload"].get("next_poll", 0) > time.time():
                        continue
                    try:
                        await self.step(task)
                    except asyncio.CancelledError:
                        raise
                    except Problem as e:
                        latest = self.store.task(task["id"])
                        if e.code in {401, 403, 409, 422}:
                            self.store.update_task(task["id"], state="needs_attention", error=e.message,
                                                   resume_state=latest["state"])
                        else:
                            failures = latest["payload"].get("failures", 0)+1
                            self.store.update_task(task["id"], error=e.message, failures=failures,
                                                   retry_at=time.time()+min(900, 15*2**min(failures, 6)))
                    except Exception:
                        latest = self.store.task(task["id"])
                        self.store.update_task(task["id"], state="needs_attention", resume_state=latest["state"],
                                               error="此阶段发生异常，文件已保留；处理后可重试")
            await asyncio.sleep(3)

    def update(self, task, state=None, **values):
        return self.store.update_task(task["id"], state=state, **values)

    async def step(self, task):
        payload, state = task["payload"], task["state"]
        settings = self.store.settings()
        staging = payload.get("staging") or str(PurePosixPath(settings["staging_root"]) / task["id"])
        if state == "queued":
            self.update(task, "preparing", staging=staging)
            return
        if state == "preparing":
            await self.openlist.mkdir(settings["staging_root"])
            await self.openlist.mkdir(staging)
            if payload["kind"] == "share":
                link_data = json.loads(await blocking(self.vault.unseal, payload["link_enc"]))
                entries = await self.p115.share_entries(link_data["link"], link_data.get("password", ""))
                requested = payload.get("selected_ids") or [entry["id"] for entry in entries]
                if not requested or not set(requested) <= {entry["id"] for entry in entries}:
                    raise Problem("分享为空或选中的内容已失效", 422)
                selected = [entry for entry in entries if entry["id"] in requested]
                share_files = await self.p115.share_tree(link_data["link"], link_data.get("password", ""), selected)
                folder_id = await self.p115.folder_id(staging)
                self.update(task, "ready_submit", selected_ids=requested, share_manifest=selected, share_files=share_files, folder_id=folder_id)
            else:
                tools = await self.openlist.request("GET", "/api/public/offline_download_tools", params={"path": staging})
                if "115 Open" not in (tools or []):
                    raise Problem("暂存目录不支持 115 Open 离线下载", 409)
                self.update(task, "ready_submit")
            return
        if state == "ready_submit":
            link_data = json.loads(await blocking(self.vault.unseal, payload["link_enc"]))
            # Persist the uncertainty boundary BEFORE performing any external write.
            baseline = [str(item.get("id")) for item in await self.openlist.remote_tasks()] if payload["kind"] == "magnet" else []
            task = self.update(task, "reconciling", submitted_at=time.time(), baseline=baseline)
            try:
                if payload["kind"] == "magnet":
                    external_id = await self.openlist.offline(link_data["link"], staging)
                    if external_id:
                        self.update(task, "downloading", external_id=str(external_id))
                else:
                    await self.p115.receive(link_data["link"], link_data.get("password", ""),
                                             payload["selected_ids"], payload["folder_id"])
                    self.update(task, "transferring", accepted=True)
            except RemoteFailure as e:
                if not e.uncertain:
                    self.update(task, "needs_attention", error=e.message, resume_state="ready_submit")
                else:
                    self.update(task, error="提交响应不确定，正在核对远端；不会重复提交")
            return
        if state == "reconciling":
            if payload["kind"] == "magnet":
                native = await self.openlist.native_status(task["resource_key"].split(":", 1)[1], staging)
                if native:
                    self.update(task, "downloading", native_folder=native["folder_id"])
                    return
                remote = await self.openlist.remote_tasks()
                matches = []
                for item in remote:
                    serialized = json.dumps(item, ensure_ascii=False)
                    if str(item.get("id")) in payload.get("baseline", []):
                        continue
                    # Never assign the first unrelated new task (other assistants also submit tasks).
                    if staging in serialized or task["id"] in serialized:
                        matches.append(item)
                if len(matches) == 1:
                    self.update(task, "downloading", external_id=str(matches[0]["id"]))
                    return
            else:
                entries = await self.openlist.entries(staging)
                expected = payload["share_manifest"]
                if all(any(e.get("name") == wanted["name"] and bool(e.get("is_dir")) == wanted["is_dir"]
                           for e in entries) for wanted in expected):
                    self.update(task, "transferring", accepted=True)
                    return
            if time.time()-payload.get("submitted_at", time.time()) > 600:
                self.update(task, "needs_attention", resume_state="reconciling",
                            error="无法确认提交结果；请检查 115／OpenList。重试只继续核对，不会再次提交")
            return
        if state == "downloading":
            native = await self.openlist.native_status(task["resource_key"].split(":", 1)[1], staging)
            if native:
                self.update(task, progress=native["progress"], native_folder=native["folder_id"])
                if native["status"] == 2:
                    self.update(task, "transferring", native_size=native["size"], accepted=True)
                elif native["status"] < 0:
                    raise Problem("115 云下载失败，请在 115 查看原因后继续", 409)
                return
            items = await self.openlist.remote_tasks()
            remote = next((t for t in items if str(t.get("id")) == payload.get("external_id")), None)
            if not remote:
                if time.time()-payload.get("submitted_at", time.time()) > 600:
                    self.update(task, "needs_attention", resume_state="downloading", error="远端任务记录不可见，请检查 OpenList")
                return
            if remote.get("error"):
                duplicate = any(term in str(remote["error"]) for term in ("任务已存在", "重复的链接"))
                message = "115 已有相同离线任务，请在 115 检查；不会移动其他任务的文件" if duplicate else "115 离线任务失败，请在 OpenList 检查或重试"
                self.update(task, "needs_attention", resume_state="downloading", error=message)
                return
            done = remote.get("bucket") == "done" or str(remote.get("state")).lower() in {"2", "completed", "success", "done"}
            self.update(task, progress=float(remote.get("progress") or remote.get("percent") or 0))
            if done:
                transfer_id = remote.get("transfer_task_id") or remote.get("transfer_id")
                self.update(task, "transferring", accepted=True, transfer_id=transfer_id)
            return
        if state == "selecting_files":
            await select_step(self, task)
            return
        if state == "placing":
            await place_step(self, task)
            return
        if state in {"transferring", "selected_ready"}:
            if state == "transferring" and payload.get("transfer_id"):
                details = await self.openlist.request("POST", "/api/task/offline_download_transfer/info",
                                                      params={"tid": payload["transfer_id"]})
                if not details or str(details.get("state")).lower() not in {"2", "completed", "success", "done"}:
                    return
                if details.get("error"):
                    raise Problem("OpenList 转移文件失败", 409)
            entries = await self.openlist.entries(staging)
            if not entries:
                if state == "selected_ready":
                    raise Problem("选中文件暂存目录为空，请核对；暂未入库", 409)
                return
            if payload["kind"] == "share":
                expected = payload["share_manifest"]
                if not all(any(e.get("name") == wanted["name"] for e in entries) for wanted in expected):
                    return
            files = await self.file_manifest(staging)
            if state == "selected_ready":
                expected = {item["path"]: item for group in payload["selection_plan"] for item in group["files"]}
                if {item["path"]: item for item in files} != expected:
                    raise Problem("选中文件暂存清单已变化，请核对；暂未入库", 409)
            if state == "transferring" and payload.get("native_size") and sum(f["size"] for f in files) != payload["native_size"]:
                return
            if payload["kind"] == "share":
                actual = {f["path"]: f["size"] for f in files}
                if not all(actual.get(e["path"]) == e["size"] for e in payload.get("share_files", [])):
                    return
            library = payload.get("pipeline") == 2
            if state == "transferring" and payload.get("magnet_selection") and not library:
                plan = map_selection(payload["magnet_selection"], files)
                selection_staging = str(PurePosixPath(settings["staging_root"]) / (task["id"] + "-selected"))
                self.update(task, "selecting_files", selection_plan=plan, download_files=files,
                            download_staging=staging, selection_staging=selection_staging,
                            selection_initialized=False, next_poll=0)
                return
            videos = [f["path"] for f in files if PurePosixPath(f["path"]).suffix.lower() in VIDEO_EXTENSIONS]
            if not videos:
                raise Problem("资源中未找到支持的影视文件，暂存内容已保留", 409)
            manifest = [{"name": e["name"], "is_dir": e.get("is_dir", False), "size": e.get("size", 0),
                         "hash_info": e.get("hash_info")} for e in entries]
            if payload.get("naming"):
                naming = {**payload["naming"], "files": files,
                          "directories": getattr(self, "manifest_directories", [])}
                if library and payload.get("magnet_selection"):
                    # The pre-download choice only marks files to keep; nothing moves before naming.
                    chosen = map_selection(payload["magnet_selection"], files)
                    naming["selected_paths"] = [item["path"] for group in chosen for item in group["files"]]
                naming, approved = bind_confirmed_preview(naming)
                task = self.update(task, "naming_planning", manifest=manifest, videos=videos, naming=naming, progress=100)
                if not approved:
                    self.pause_naming(task, naming)
            else:
                self.update(task, "organizing", manifest=manifest, videos=videos, progress=100)
            return
        if state == "naming_planning":
            await self.verify_naming_manifest(task)
            naming = build_plan(payload["naming"])
            task = self.update(task, naming=naming)
            if (model_candidates(naming) and settings["llm_enabled"]
                    and not naming.get("llm")):
                self.start_naming_analysis(task)
                return
            if naming.get("review_required"):
                self.pause_naming(task, naming)
                return
            if naming["errors"]:
                raise Problem("；".join(naming["errors"]), 409)
            self.update(task, "renaming", naming=naming, next_poll=0)
            return
        if state == "naming_analyzing":
            if task["id"] not in self.naming_jobs:
                naming = payload["naming"]
                naming["llm"] = {"status": "failed", "message": "上次识别已中断，请人工补全或重新识别"}
                naming["review_required"] = True
                self.update(task, "needs_attention", naming=naming, resume_state="naming_planning",
                            error=naming["llm"]["message"], failures=0, retry_at=0)
            return
        if state == "renaming":
            await self.rename_step(task, staging)
            return
        if state == "organizing":
            entries = await self.openlist.entries(task["target"])
            conflicts = {e["name"] for e in entries} & {e["name"] for e in payload["manifest"]}
            if conflicts:
                raise Problem("目标存在同名内容，暂存文件已保留；请选择另一个媒体子目录", 409)
            self.update(task, "moving", move_attempted_at=time.time())
            try:
                await self.openlist.move(staging, task["target"], [e["name"] for e in payload["manifest"]])
            except RemoteFailure as e:
                if not e.uncertain:
                    self.update(task, "organizing", error="移动未成功，稍后重试")
            return
        if state == "moving":
            src, dst = await self.openlist.entries(staging), await self.openlist.entries(task["target"])
            wanted = {e["name"] for e in payload["manifest"]}
            src_names, dst_names = {e["name"] for e in src}, {e["name"] for e in dst}
            if wanted.isdisjoint(src_names) and wanted <= dst_names:
                relative = str(PurePosixPath(task["target"]).relative_to(settings["media_root"]))
                self.update(task, "scan_queued", scan_path=str(PurePosixPath(settings["strm_root"]) / relative))
            elif wanted <= src_names and wanted.isdisjoint(dst_names):
                self.update(task, "organizing")
            elif time.time()-payload.get("move_attempted_at", time.time()) > 180:
                raise Problem("移动结果不完整或目标发生冲突，请检查文件；不会自动覆盖", 409)
            return
        if state == "scan_queued":
            status = await self.openlist.scan_status()
            if not status.get("is_done", False):
                return
            self.update(task, "scanning", scan_started=time.time())
            try:
                resolved = await self.openlist.scan(payload["scan_path"])
                if resolved:
                    self.update(task, scan_path=resolved)
            except RemoteFailure as e:
                if not e.uncertain:
                    self.update(task, "scan_queued", error="扫描忙碌，等待再次启动")
            return
        if state == "scanning":
            status = await self.openlist.scan_status()
            if not status.get("is_done", False):
                return
            if not await blocking(self.strm_ready, task):
                if time.time()-payload.get("scan_started", time.time()) > 90:
                    self.update(task, "scan_queued", error="对应 STRM 尚未完整生成，重新排队扫描", retry_at=time.time()+60)
                return
            self.update(task, "notifying")
            return
        if state == "notifying":
            await refresh_emby(settings, self.vault)
            self.update(task, "completed", progress=100, notified_at=time.time())

    async def file_manifest(self, root):
        output, pending = [], [(root, "")]
        self.manifest_directories = []
        visited = 0
        while pending:
            directory, relative = pending.pop()
            visited += 1
            entries = await self.openlist.entries(directory)
            for e in entries:
                name = e.get("name", "")
                if not name or "\x00" in name or "/" in name or "\\" in name or name in {".", ".."}:
                    raise Problem("资源包含不安全文件名", 422)
                rel = str(PurePosixPath(relative) / name)
                if e.get("is_dir"):
                    self.manifest_directories.append(rel)
                    pending.append((str(PurePosixPath(directory) / name), rel))
                else:
                    output.append({"path": rel, "size": int(e.get("size") or 0),
                                   "identity": {k: str(e[k]) for k in ("id", "hash_info") if e.get(k)}})
            if len(output)+len(pending)+visited > 20000:
                raise Problem("单次资源规模超过限制，请在分享预览中选择较小的范围", 422)
        return output

    @staticmethod
    def naming_identity_matches(entry, item):
        if not entry or entry.get("is_dir") or int(entry.get("size") or 0) != item["size"]:
            return False
        return all(str(entry.get(key)) == value for key, value in item.get("identity", {}).items())

    async def rename_step(self, task, staging):
        naming = task["payload"]["naming"]
        if naming.get("review_required"):
            raise Problem("请先在任务详情确认当前命名预览", 409)
        if naming.get("llm"):
            if naming.get("confirmed_revision") != naming.get("revision"):
                raise Problem("请先在任务详情确认当前命名预览", 409)
            if not naming.get("locked"):
                await self.verify_naming_manifest(task)
        for item in naming["plan"]:
            if item["status"] == "done":
                continue
            source, target = PurePosixPath(item["source"]), PurePosixPath(item["target"])
            directory = str(PurePosixPath(staging) / source.parent)
            entries = await self.openlist.entries(directory)
            src = next((e for e in entries if e["name"] == source.name), None)
            dst = next((e for e in entries if e["name"] == target.name), None)
            if source == target:
                if not self.naming_identity_matches(src, item):
                    raise Problem("暂存文件已变化，请核对后重试", 409)
                item["status"] = "done"
                self.update(task, naming=naming)
                return
            if item["status"] == "attempted":
                if src is None and self.naming_identity_matches(dst, item) and (item.get("acknowledged") or item.get("identity")):
                    item["status"] = "done"
                    self.update(task, naming=naming)
                    return
                # No reliable remote identity means an uncertain result cannot be inferred from size alone.
                if src is None or dst is not None or not self.naming_identity_matches(src, item):
                    raise Problem("重命名结果无法确认，请检查暂存文件；不会覆盖或重复提交", 409)
                if time.time() - item["attempted_at"] < 180:
                    self.update(task, next_poll=time.time() + self.store.settings()["poll_seconds"])
                    return
                raise Problem("重命名响应不确定且原文件仍在，请在远端完成该项后重试核对", 409)
            if dst is not None or not self.naming_identity_matches(src, item):
                raise Problem("暂存文件已变化或目标名称被占用，请检查文件", 409)
            naming["locked"] = True
            item.update(status="attempted", attempted_at=time.time())
            self.update(task, naming=naming)
            try:
                await self.openlist.rename(str(PurePosixPath(staging) / source), target.name)
            except RemoteFailure as exc:
                if not exc.uncertain:
                    item["status"] = "pending"
                    self.update(task, naming=naming)
                    raise
                # Persist the response uncertainty so a restart follows the same reconciliation path.
                item["uncertain"] = True
                self.update(task, naming=naming, next_poll=time.time() + self.store.settings()["poll_seconds"])
                return
            item["acknowledged"] = True
            self.update(task, naming=naming)
            return
        # Verify the entire final tree before starting the existing move/scan pipeline.
        files = await self.file_manifest(staging)
        actual = {f["path"]: f for f in files}
        mapping = {item["source"]: item["target"] for item in naming["plan"]}
        expected = {mapping.get(f["path"], f["path"]): f for f in naming["files"]}
        if set(actual) != set(expected) or set(self.manifest_directories) != set(naming.get("directories", [])):
            raise Problem("重命名后的文件清单发生变化，请检查暂存内容", 409)
        for path, original in expected.items():
            if actual[path]["size"] != original["size"] or actual[path]["identity"] != original.get("identity", {}):
                raise Problem("重命名后的文件身份或大小不符，请检查暂存内容", 409)
        entries = await self.openlist.entries(staging)
        manifest = [{"name": e["name"], "is_dir": e.get("is_dir", False), "size": e.get("size", 0),
                     "hash_info": e.get("hash_info")} for e in entries]
        naming["locked"] = True
        if task["payload"].get("pipeline") == 2:
            self.update(task, "placing", naming=naming, placement=placement_plan(naming["plan"], files),
                        placement_initialized=False, next_poll=0)
            return
        videos = [item["target"] for item in naming["plan"] if item["kind"] == "video"]
        self.update(task, "organizing", naming=naming, manifest=manifest, videos=videos)

    def strm_ready(self, task):
        settings = self.store.settings()
        # OpenList's STRM driver writes paths relative to its mounted source root.
        root = Path(settings["strm_local"])
        relative = PurePosixPath(task["target"]).relative_to(settings["mount_root"])
        target = root.joinpath(*relative.parts)
        for video in task["payload"]["videos"]:
            path = target.joinpath(*PurePosixPath(video).parts)
            alternatives = (path.with_suffix(".strm"), Path(str(path)+".strm"))
            if not any(p.is_file() and p.stat().st_size > 0 for p in alternatives):
                return False
        return True

    async def retry(self, task_id, new_target=None):
        async with self.lock:
            task = self.store.task(task_id)
            if task["state"] == "completed":
                raise Problem("任务已完成", 409)
            if task["state"] == "canceled":
                raise Problem("已取消任务不会自动重新提交；请先检查 115 中的任务和暂存内容", 409)
            if task["state"] in {"naming_review", "naming_analyzing"}:
                return task
            if task["payload"].get("resume_state") == "downloading" and task["payload"].get("external_id"):
                native = await self.openlist.native_status(task["resource_key"].split(":", 1)[1], task["payload"]["staging"])
                if native:
                    return self.update(task, "downloading", failures=0, retry_at=0, next_poll=0)
                remote = next((r for r in await self.openlist.remote_tasks() if str(r.get("id")) == task["payload"]["external_id"]), None)
                if remote and remote.get("error"):
                    if any(term in str(remote["error"]) for term in ("任务已存在", "重复的链接")):
                        raise Problem("115 已存在该任务，请先在 115 中核查；不能安全自动重新提交", 409)
                    await self.openlist.request("POST", "/api/task/offline_download/retry", params={"tid": task["payload"]["external_id"]})
            if new_target:
                from .core import target_path
                new_target = target_path(new_target, self.store.settings()["media_root"])
                resume = task["payload"].get("resume_state")
                placed = any(item["status"] != "pending" for item in task["payload"].get("placement", []))
                if resume not in {"organizing", "placing"} or (resume == "placing" and placed):
                    raise Problem("只能在同名冲突阶段更换目标目录", 409)
                await self.openlist.get(new_target)
                with self.store.connect() as db:
                    try:
                        db.execute("UPDATE tasks SET target=? WHERE id=?", (new_target, task_id))
                    except Exception:
                        raise Problem("新目录已有同资源任务", 409) from None
            changes = {}
            if task["state"] == "needs_attention" and task["payload"].get("resume_state") == "selecting_files":
                changes["selection_plan"] = await retry_selection_plan(self, task)
            if task["state"] == "needs_attention" and task["payload"].get("resume_state") == "placing":
                if new_target:
                    changes["placement_initialized"] = False
                else:
                    changes["placement"] = await retry_placement_plan(self, task)
            return self.update(task, task["payload"].get("resume_state") or task["state"],
                               failures=0, retry_at=0, next_poll=0, **changes)

    async def cancel(self, task_id):
        async with self.lock:
            task = self.store.task(task_id)
            if task["state"] == "completed":
                raise Problem("任务已完成", 409)
            self.cancel_naming_job(task_id)
            naming = task["payload"].get("naming")
            if naming and naming.get("llm", {}).get("status") == "running":
                naming["llm"] = {"status": "canceled", "message": "大模型识别已停止"}
            if task["payload"].get("external_id") and task["state"] in {"downloading", "reconciling"}:
                await self.openlist.cancel(task["payload"]["external_id"])
            return self.update(task, "canceled", naming=naming,
                               error="本地流程已停止，已生成文件保留；不确定的远端提交请在 115 检查")
