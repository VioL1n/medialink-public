import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from medialink.core import Problem
from medialink.magnets import preview_snapshot
from playwright.sync_api import expect, sync_playwright
from test_magnets import FILES, HASH
from test_naming_browser import configure
from test_settings_browser import sign_in

pytest_plugins = ["test_settings_browser", "test_medialink"]
pytestmark = pytest.mark.skipif(os.environ.get("MEDIALINK_BROWSER_TESTS") != "1", reason="requires Chromium and isolated build")


def open_manual(page, infohash=HASH):
    page.get_by_role("button", name="资源搜索", exact=True).click()
    page.get_by_role("button", name="粘贴链接", exact=True).click()
    page.get_by_label("磁力链接或 115 分享").fill("magnet:?xt=urn:btih:" + infohash)
    page.get_by_role("button", name="选择入库目录", exact=True).click()


def test_magnet_tree_selection_and_reopen(browser_app):
    base, app, store, tmp_path = browser_app
    configure(app, store)
    data = preview_snapshot(HASH, "合集", FILES)
    app.state.magnets.resolve = AsyncMock(return_value=data)
    app.state.openlist.offline = AsyncMock()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        submissions, errors = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: submissions.append(request.post_data_json) if request.method == "POST" and request.url.endswith("/api/v1/tasks") else None)
        sign_in(page, base, setup=True)
        open_manual(page)
        expect(page.get_by_role("status").filter(has_text="已选 3 / 4")).to_be_visible()
        root = page.get_by_role("checkbox", name="选择目录 合集", exact=True)
        expect(root).to_have_attribute("aria-checked", "mixed")
        expect(page.get_by_role("checkbox", name="选择文件 合集/README.txt", exact=True)).not_to_be_checked()
        expect(page.get_by_role("checkbox", name="自动规范命名", exact=True)).to_be_checked()
        # Without TMDB, a library ingest cannot be named; this test covers the keep-original-names path.
        expect(page.get_by_role("button", name="确认入库", exact=True)).to_be_disabled()
        page.get_by_role("checkbox", name="自动规范命名", exact=True).uncheck()
        assert not store.tasks()
        app.state.openlist.offline.assert_not_awaited()
        page.get_by_role("button", name="收起目录 合集/季二", exact=True).click()
        expect(page.get_by_role("checkbox", name="选择文件 合集/季二/第一集.mkv", exact=True)).not_to_be_visible()
        page.get_by_role("button", name="展开目录 合集/季二", exact=True).click()
        page.get_by_role("button", name="全选", exact=True).click()
        expect(root).to_be_checked()
        page.get_by_role("button", name="清空", exact=True).click()
        expect(page.get_by_role("button", name="确认入库", exact=True)).to_be_disabled()
        page.get_by_role("checkbox", name="选择文件 合集/季一/第一集.zh.srt", exact=True).check()
        expect(page.get_by_role("button", name="确认入库", exact=True)).to_be_disabled()
        page.get_by_role("checkbox", name="选择目录 合集/季一", exact=True).check()
        expect(page.get_by_role("button", name="确认入库", exact=True)).to_be_enabled()
        expect(page.get_by_role("status").filter(has_text="已选 2 / 4")).to_be_visible()
        page.screenshot(path=str(tmp_path / "magnet-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        page.screenshot(path=str(tmp_path / "magnet-mobile.png"), full_page=True)
        page.locator(".modal-close").click()
        open_manual(page)
        expect(page.get_by_role("status").filter(has_text="已选 3 / 4")).to_be_visible()
        page.get_by_role("checkbox", name="自动规范命名", exact=True).uncheck()
        page.get_by_role("checkbox", name="选择目录 合集/季二", exact=True).uncheck()
        page.get_by_role("button", name="确认入库", exact=True).click()
        expect(page.get_by_text("任务已添加，可在入库任务中查看", exact=True)).to_be_visible()
        assert set(submissions[-1]["magnet_selection"]["selected_ids"]) == {data["files"][0]["id"], data["files"][2]["id"]}
        assert submissions[-1]["auto_naming"] is False
        assert store.tasks()[0]["payload"]["magnet_selection"]["total_count"] == 4
        page.get_by_role("button", name="查看详情", exact=True).click()
        expect(page.get_by_text("选中 2 个文件入库，2 个未选文件保留在下载暂存目录。", exact=True)).to_be_visible()
        app.state.openlist.offline.assert_not_awaited()
        assert not errors
        browser.close()


def test_preview_failure_never_blocks_submission_and_can_retry(browser_app):
    base, app, store, tmp_path = browser_app
    configure(app, store)
    app.state.magnets.resolve = AsyncMock(side_effect=Problem("磁力列表解析超时", 504))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        sign_in(page, base, setup=True)
        open_manual(page)
        expect(page.get_by_text("磁力列表解析超时", exact=True)).to_be_visible()
        page.get_by_role("checkbox", name="自动规范命名", exact=True).uncheck()
        expect(page.get_by_role("button", name="确认入库", exact=True)).to_be_enabled()
        assert not store.tasks()
        app.state.magnets.resolve = AsyncMock(return_value=preview_snapshot(HASH, "合集", FILES))
        page.get_by_role("button", name="重试解析", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="已选 3 / 4")).to_be_visible()
        page.locator(".modal-close").click()
        app.state.magnets.resolve = AsyncMock(side_effect=Problem("磁力列表解析超时", 504))
        open_manual(page, "b" * 40)
        expect(page.get_by_text("磁力列表解析超时", exact=True)).to_be_visible()
        page.get_by_role("checkbox", name="自动规范命名", exact=True).uncheck()
        expect(page.get_by_role("button", name="确认入库", exact=True)).to_be_enabled()
        assert not store.tasks()
        page.get_by_role("button", name="确认入库", exact=True).click()
        expect(page.get_by_text("任务已添加，可在入库任务中查看", exact=True)).to_be_visible()
        assert store.tasks()[0]["payload"]["magnet_selection"] is None
        browser.close()


def test_closing_preview_keeps_resolving_and_new_link_has_fresh_selection(browser_app):
    base, app, store, tmp_path = browser_app
    configure(app, store)
    started, cancelled = [], []

    async def resolve(link, key):
        started.append(key)
        if key.endswith(HASH):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(key)
        return preview_snapshot(key.split(":", 1)[1], None, [{"path": "新的影片.mkv", "size": 100}])

    app.state.magnets.resolve = resolve
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        sign_in(page, base, setup=True)
        open_manual(page)
        expect(page.get_by_text("正在解析文件列表，可直接确认入库…", exact=True)).to_be_visible()
        page.get_by_role("checkbox", name="自动规范命名", exact=True).uncheck()
        expect(page.get_by_role("button", name="确认入库", exact=True)).to_be_enabled()
        page.locator(".modal-close").click()
        open_manual(page, "b" * 40)
        expect(page.get_by_role("checkbox", name="选择文件 新的影片.mkv", exact=True)).to_be_checked()
        expect(page.get_by_role("status").filter(has_text="已选 1 / 1")).to_be_visible()
        # Closing the modal releases the request; the bounded resolution keeps
        # going so reopening the same link resumes instead of starting over.
        assert "magnet:" + HASH in started and "magnet:" + HASH not in cancelled
        assert "magnet:" + HASH in app.state.magnets.pending
        assert not store.tasks()
        browser.close()


def test_slow_preview_resumes_automatically_after_request_timeout(browser_app):
    base, app, store, tmp_path = browser_app
    configure(app, store)
    started = []

    async def resolve(link, key):
        started.append(key)
        await asyncio.sleep(1)
        return preview_snapshot(HASH, "合集", FILES)

    app.state.magnets.resolve = resolve
    app.state.magnets.timeout = 0.2
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        responses = []
        page.on("response", lambda response: "/magnets/preview" in response.url and responses.append(response.status))
        sign_in(page, base, setup=True)
        open_manual(page)
        expect(page.get_by_role("status").filter(has_text="已选 3 / 4")).to_be_visible()
        expect(page.get_by_role("alert")).to_have_count(0)
        assert started == ["magnet:" + HASH]
        assert 504 in responses and responses[-1] == 200
        browser.close()


def test_library_ingest_submits_while_preview_is_still_loading(browser_app):
    base, app, store, tmp_path = browser_app
    configure(app, store)

    async def resolve(link, key):
        await asyncio.Event().wait()

    app.state.magnets.resolve = resolve
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        sign_in(page, base, setup=True)
        open_manual(page)
        expect(page.get_by_text("正在解析文件列表，可直接确认入库…", exact=True)).to_be_visible()
        expect(page.get_by_role("checkbox", name="自动规范命名", exact=True)).to_be_checked()
        expect(page.get_by_role("button", name="确认入库", exact=True)).to_be_disabled()
        page.get_by_label("TMDB 影片关键词").fill("星际穿越")
        page.get_by_role("button", name="搜索 TMDB", exact=True).click()
        page.get_by_role("button", name="星际穿越 · 年份未知 · 电影", exact=True).click()
        expect(page.get_by_text("将创建：/115/infuse/星际穿越 [tmdbid=157336]", exact=True)).to_be_visible()
        expect(page.get_by_text("入库分类目录", exact=False)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        page.screenshot(path=str(tmp_path / "library-mobile.png"), full_page=True)
        page.get_by_role("button", name="确认入库", exact=True).click()
        expect(page.get_by_text("任务已添加，可在入库任务中查看", exact=True)).to_be_visible()
        payload = store.tasks()[0]["payload"]
        assert payload["pipeline"] == 2 and payload["naming"]["version"] == 2 and payload["magnet_selection"] is None
        assert not errors
        browser.close()
