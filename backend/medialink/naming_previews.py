"""Trusted, cancellable naming drafts. Preparing a draft never writes cloud files."""
import asyncio
import copy
import hashlib
import json
import time
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from .core import Problem, blocking, resource_key
from .llm import BATCH_SIZE
from .magnets import preview_snapshot
from .naming import build_plan, media_snapshot, model_candidates, normalize_resolution, public_naming

TTL = 1800


def request_binding(data):
    selection = data.get("magnet_selection")
    canonical = {"resource": resource_key(data["link"]),
                 "password": data.get("password") or parse_qs(urlparse(data["link"]).query).get("password", [""])[0],
                 "selected_ids": sorted(set(data.get("selected_ids", []))), "media": data.get("media"),
                 "resolution": normalize_resolution(data.get("resolution")),
                 "magnet_selection": {"preview_id": selection["preview_id"],
                                      "selected_ids": sorted(set(selection["selected_ids"]))} if selection else None}
    if canonical["media"]:
        canonical["media"] = {key: canonical["media"].get(key) for key in ("type", "id", "season_number")}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


class NamingPreviews:
    def __init__(self, store, vault, magnets, p115, tmdb, llm):
        self.store, self.vault, self.magnets = store, vault, magnets
        self.p115, self.tmdb, self.llm = p115, tmdb, llm
        self.jobs = {}
        self.slots = asyncio.Semaphore(2)

    @staticmethod
    def key(identifier):
        return "naming-preview:" + identifier

    def save(self, draft):
        draft["revision"] += 1
        draft["naming"]["revision"] = draft["revision"]
        self.store.cache_metadata(self.key(draft["preview_id"]), draft, TTL)
        return draft

    def current(self, identifier, run_id=None):
        cached = self.store.cached_metadata(self.key(identifier))
        if not cached or cached["expires"] <= time.time():
            return None
        draft = cached["data"]
        return draft if run_id is None or draft.get("run_id") == run_id else None

    def get(self, identifier):
        draft = self.current(identifier)
        if draft is None:
            self.cancel(identifier)
            raise Problem("命名预览已取消或过期，请重新生成", 409)
        if draft["status"] in {"preparing", "analyzing"} and identifier not in self.jobs:
            draft["status"] = "failed"
            draft["error"] = "上次命名预览已中断，请重新识别"
            draft["naming"]["llm"] = {"status": "failed", "message": draft["error"]}
            self.save(draft)
        return draft

    @staticmethod
    def public(draft):
        naming = public_naming({"state": "naming_review", "payload": {"naming": draft["naming"]}})
        return {"preview_id": draft["preview_id"], "revision": draft["revision"], "status": draft["status"],
                "error": draft.get("error"), "naming": naming}

    def cancel(self, identifier):
        job = self.jobs.pop(identifier, None)
        if job:
            job.cancel()

    def delete(self, identifier):
        self.cancel(identifier)
        with self.store.connect() as db:
            db.execute("DELETE FROM metadata_cache WHERE key=?", (self.key(identifier),))

    def launch(self, identifier, coroutine):
        job = asyncio.create_task(coroutine)
        self.jobs[identifier] = job

        def finished(done):
            if self.jobs.get(identifier) is done:
                self.jobs.pop(identifier, None)

        job.add_done_callback(finished)

    async def create(self, data):
        if data.get("magnet_selection") and not resource_key(data["link"]).startswith("magnet:"):
            raise Problem("文件预览选择仅用于磁力链接", 422)
        if not data.get("media"):
            raise Problem("请先关联 TMDB 电影或剧集，再生成命名预览", 422)
        if not self.store.settings()["llm_enabled"]:
            raise Problem("请先在服务连接中启用大模型辅助命名", 409)
        identifier, run_id = uuid.uuid4().hex, uuid.uuid4().hex
        secret = await blocking(self.vault.seal, json.dumps(data))
        draft = {"preview_id": identifier, "run_id": run_id, "status": "preparing", "revision": 0,
                 "binding": request_binding(data), "request_enc": secret, "root": None, "selection": None,
                 "naming": {"version": 2, "files": [], "plan": [], "overrides": {}, "locked": False,
                            "review_required": True}, "error": None}
        self.save(draft)
        self.launch(identifier, self.prepare(identifier, run_id))
        return draft

    async def prepare(self, identifier, run_id):
        try:
            async with self.slots:
                draft = self.current(identifier, run_id)
                if draft is None:
                    return
                data = json.loads(await blocking(self.vault.unseal, draft["request_enc"]))
                key = resource_key(data["link"])
                root, selection, selected_paths = None, None, None
                if key.startswith("magnet:"):
                    if data.get("magnet_selection"):
                        selection = self.magnets.select(key, SimpleNamespace(**data["magnet_selection"]))
                        # The whole torrent is downloaded; the selection only decides what is kept.
                        files, root = self.magnets.cached(key)["files"], selection["root"]
                        selected_paths = [item["path"] for item in selection["files"]]
                    else:
                        preview = await self.magnets.preview(data["link"], wait=None)
                        files, root = preview["files"], preview["root"]
                else:
                    entries = await self.p115.share_entries(data["link"], data.get("password", ""))
                    ids = set(data.get("selected_ids", []))
                    if ids and not ids <= {entry["id"] for entry in entries}:
                        raise Problem("选中的分享内容已失效，请重新预览", 409)
                    selected = [entry for entry in entries if not ids or entry["id"] in ids]
                    files = await self.p115.share_tree(data["link"], data.get("password", ""), selected)
                snapshot = preview_snapshot(key, root, files)
                media = data["media"]
                detail = await self.tmdb.detail(media["type"], media["id"])
                draft = self.current(identifier, run_id)
                if draft is None:
                    return
                draft.update(root=snapshot["root"], selection=selection)
                draft["naming"] = build_plan({**draft["naming"], "media": media_snapshot(detail),
                    "season_number": media.get("season_number"), "resolution": normalize_resolution(data.get("resolution")),
                    "selected_paths": selected_paths,
                    "files": [{"path": item["path"], "size": item["size"]} for item in snapshot["files"]]})
                self.save(draft)
                await self.analyze(identifier, run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fail(identifier, run_id, exc)

    def fail(self, identifier, run_id, exc):
        draft = self.current(identifier, run_id)
        if draft:
            message = exc.message if isinstance(exc, Problem) else "命名预览失败，请重新识别或人工补全"
            draft.update(status="failed", error=message)
            draft["naming"]["llm"] = {"status": "failed", "message": message}
            self.save(draft)

    async def analyze(self, identifier, run_id):
        draft = self.current(identifier, run_id)
        if draft is None:
            return
        candidates = model_candidates(draft["naming"])
        draft.update(status="analyzing" if candidates else "ready", error=None)
        draft["naming"]["llm"] = {"status": "running" if candidates else "completed",
                                  "message": "正在识别命名信息" if candidates else "命名预览已生成，请核对后确认入库"}
        self.save(draft)
        try:
            for offset in range(0, len(candidates), BATCH_SIZE):
                result = await self.llm.suggest(draft["naming"], candidates[offset:offset + BATCH_SIZE])
                draft = self.current(identifier, run_id)
                if draft is None:
                    return
                draft["naming"]["suggestions"] = draft["naming"].get("suggestions", {}) | result
                draft["naming"] = build_plan(draft["naming"])
                self.save(draft)
            draft = self.current(identifier, run_id)
            if draft:
                draft.update(status="ready", error=None)
                draft["naming"]["llm"] = {"status": "completed", "message": "命名预览已生成，请核对后确认入库"}
                self.save(draft)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fail(identifier, run_id, exc)

    def reanalyze(self, identifier):
        if not self.store.settings()["llm_enabled"]:
            raise Problem("请先在服务连接中启用大模型辅助命名", 409)
        draft = self.get(identifier)
        self.cancel(identifier)
        run_id = uuid.uuid4().hex
        draft["run_id"] = run_id
        if not draft["naming"]["files"]:
            draft.update(status="preparing", error=None)
            self.save(draft)
            self.launch(identifier, self.prepare(identifier, run_id))
        else:
            if draft["naming"].get("llm", {}).get("status") == "completed":
                draft["naming"]["suggestions"] = {}
            draft["naming"] = build_plan(draft["naming"])
            draft.update(status="analyzing", error=None)
            self.save(draft)
            self.launch(identifier, self.analyze(identifier, run_id))
        return draft

    def correct(self, identifier, data):
        from .naming import apply_file_corrections

        draft = self.get(identifier)
        if data.revision != draft["revision"]:
            raise Problem("命名预览已更新，请刷新后再保存", 409)
        if not draft["naming"]["files"]:
            raise Problem("文件清单尚未到位，请稍后再保存", 409)
        if "media" in data.model_fields_set or "resolution" in data.model_fields_set:
            raise Problem("请在入库弹窗更换影视或清晰度并重新生成预览", 422)
        naming = copy.deepcopy(draft["naming"])
        apply_file_corrections(naming, data.files)
        self.cancel(identifier)
        draft["run_id"] = uuid.uuid4().hex
        draft["naming"] = build_plan(naming)
        draft["naming"]["llm"] = {"status": "superseded", "message": "命名修正已保存，请核对后确认入库"}
        draft.update(status="ready", error=None)
        return self.save(draft)

    def confirm(self, identifier, revision, data):
        draft = self.get(identifier)
        if not data.get("auto_naming") or draft["binding"] != request_binding(data):
            raise Problem("入库信息与命名预览不一致，请重新生成预览", 409)
        if revision != draft["revision"] or draft["status"] != "ready" or draft["naming"].get("errors"):
            raise Problem("命名预览已更新或尚未完成，请核对当前预览", 409)
        naming = copy.deepcopy(draft["naming"])
        naming.update(review_required=False, confirmed_revision=revision)
        naming["preview"] = {"preview_id": identifier, "revision": revision, "confirmed": True,
                             "root": draft["root"], "files": copy.deepcopy(naming["files"]),
                             "targets": {item["source"]: item["target"] for item in naming["plan"]},
                             "destinations": {item["source"]: item["destination"] for item in naming["plan"]
                                              if item.get("destination")},
                             "context": [{"source": item["source"], "values": {key: item[key] for key in
                                ("season_number", "episode_number", "resolution") if item.get(key) is not None
                                and key not in naming.get("suggestions", {}).get(item["id"], {})
                                and key not in naming.get("overrides", {}).get(item["id"], {})}}
                                for item in naming["plan"] if item["kind"] == "video"]}
        return naming, copy.deepcopy(draft["selection"])

    async def close(self):
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self.jobs.clear()
