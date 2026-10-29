import os
from unittest.mock import AsyncMock

import pytest
from medialink.naming import build_plan
from playwright.sync_api import expect, sync_playwright
from test_naming import MOVIE, TV
from test_settings_browser import sign_in

pytest_plugins = ["test_settings_browser", "test_medialink"]
pytestmark = pytest.mark.skipif(os.environ.get("MEDIALINK_BROWSER_TESTS") != "1", reason="requires Chromium and isolated frontend build")


def configure(app, store):
    store.configure({"enabled_search_sources": ["nyaa"]})
    app.state.openlist.get = AsyncMock(return_value={"is_dir": True})
    app.state.openlist.entries = AsyncMock(return_value=[])
    movie = {**MOVIE, "votes": 0, "overview": "测试资料", "updated": 1, "stale": False}
    tv = {**TV, "votes": 0, "overview": "测试资料", "updated": 1, "stale": False, "seasons": [{"number": 1, "name": "第一季", "episodes": 7}]}

    async def detail(kind, identifier):
        return movie if kind == "movie" else tv

    app.state.tmdb.detail = AsyncMock(side_effect=detail)
    app.state.tmdb.search = AsyncMock(return_value={"items": [movie], "updated": 1, "stale": False})
    app.state.nyaa.search = AsyncMock(return_value=[{"key": "magnet:" + "a" * 40, "kind": "magnet", "title": "Interstellar 2014 4K", "link": "magnet:?xt=urn:btih:" + "a" * 40, "resolution": "4K", "sources": [{"provider": "nyaa", "name": "Nyaa", "url": "https://nyaa.si/view/1"}]}])


@pytest.mark.parametrize("auto_naming", [False, True])
@pytest.mark.parametrize("entry", ["detail", "search", "manual", "tv"])
def test_ingestion_entries_respect_auto_naming(browser_app, entry, auto_naming):
    base, app, store, tmp_path = browser_app
    configure(app, store)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        submissions, errors = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: submissions.append(request.post_data_json) if request.method == "POST" and request.url.endswith("/api/v1/tasks") else None)
        sign_in(page, base, setup=True)
        if entry in {"detail", "tv"}:
            page.goto(base + ("/#/media/movie/157336" if entry == "detail" else "/#/media/tv/1396"))
            if entry == "tv":
                page.get_by_label("剧集季数").select_option("1")
            page.get_by_role("button", name="入库 115", exact=True).click()
        else:
            page.get_by_role("button", name="资源搜索", exact=True).click()
            if entry == "search":
                page.get_by_label("影视资源关键词").fill("星际穿越")
                page.get_by_role("button", name="搜索资源", exact=True).click()
                page.get_by_role("button", name="入库 115", exact=True).click()
            else:
                page.get_by_role("button", name="粘贴链接", exact=True).click()
                page.get_by_label("磁力链接或 115 分享").fill("magnet:?xt=urn:btih:" + "a" * 40)
                page.get_by_role("button", name="选择入库目录", exact=True).click()
        checkbox = page.get_by_role("checkbox", name="自动规范命名", exact=True)
        expect(checkbox).to_be_checked()
        if not auto_naming:
            checkbox.uncheck()
            expect(page.get_by_text("关联 TMDB 影片", exact=True)).not_to_be_visible()
        else:
            if entry == "manual":
                page.get_by_label("TMDB 影片关键词").fill("星际穿越")
                page.get_by_role("button", name="搜索 TMDB", exact=True).click()
                page.get_by_role("button", name="星际穿越 · 年份未知 · 电影", exact=True).click()
                page.get_by_label("入库电影清晰度").select_option("2160P")
            expect(page.get_by_role("button", name="大模型规范命名", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="确认入库", exact=True)).to_be_enabled()
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        page.get_by_role("button", name="确认入库", exact=True).click()
        expect(page.get_by_text("任务已添加，可在入库任务中查看", exact=True)).to_be_visible()
        assert submissions[-1]["auto_naming"] is auto_naming
        if auto_naming:
            assert submissions[-1]["media"]["type"] == ("tv" if entry == "tv" else "movie")
            assert submissions[-1]["media"]["id"] == (1396 if entry == "tv" else 157336)
            if entry == "tv":
                assert submissions[-1]["media"]["season_number"] == 1
            else:
                assert submissions[-1]["resolution"] == ("2160P" if entry == "manual" else "4K")
            assert store.tasks()[0]["payload"]["naming"]["media"]["original_title"] == ("Breaking Bad" if entry == "tv" else "Interstellar")
        else:
            assert "media" not in submissions[-1]
            assert "resolution" not in submissions[-1]
            assert store.tasks()[0]["payload"]["naming"] is None
        page.get_by_role("button", name="资源搜索", exact=True).click()
        page.get_by_role("button", name="粘贴链接", exact=True).click()
        page.get_by_label("磁力链接或 115 分享").fill("magnet:?xt=urn:btih:" + "b" * 40)
        page.get_by_role("button", name="选择入库目录", exact=True).click()
        expect(page.get_by_role("checkbox", name="自动规范命名", exact=True)).to_be_checked()
        assert not errors
        browser.close()


def test_task_correction_preview_survives_poll_and_resumes(browser_app):
    base, app, store, tmp_path = browser_app
    configure(app, store)
    naming = build_plan({"version": 1, "media": TV, "files": [{"path": "episode.mkv", "size": 12}], "overrides": {}, "locked": False})
    task = store.create_task("magnet:" + "b" * 40, "/115/infuse", {"kind": "magnet", "title": "待补全剧集", "naming": naming})
    store.update_task(task["id"], "needs_attention", error="缺少季集号", resume_state="naming_planning")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        sign_in(page, base, setup=True)
        page.get_by_role("button", name="入库任务", exact=True).click()
        page.get_by_role("button", name="查看详情", exact=True).click()
        expect(page.get_by_text("→ 等待补全", exact=True)).to_be_visible()
        page.get_by_label("episode.mkv 季号", exact=True).fill("0")
        page.get_by_label("episode.mkv 集号", exact=True).fill("1")
        # The background task polling response must not discard edits.
        with page.expect_response(lambda response: response.url.endswith("/api/v1/tasks/" + task["id"]) and response.request.method == "GET", timeout=8000):
            pass
        expect(page.get_by_label("episode.mkv 季号", exact=True)).to_have_value("0")
        expect(page.get_by_label("episode.mkv 集号", exact=True)).to_have_value("1")
        page.get_by_role("button", name="保存命名信息并继续", exact=True).click()
        expect(page.get_by_text("→ 绝命毒师 S00E01.mkv", exact=True)).to_be_visible()
        assert store.task(task["id"])["state"] == "naming_planning"
        page.screenshot(path=str(tmp_path / "naming-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        page.screenshot(path=str(tmp_path / "naming-mobile.png"), full_page=True)
        assert not errors
        browser.close()


def test_library_task_detail_lists_and_restores_excluded_files(browser_app):
    from test_curation import AD, JACKPOT, jackpot
    from test_curation import TV as SERIES

    base, app, store, tmp_path = browser_app
    configure(app, store)
    naming = build_plan({"version": 2, "media": SERIES, "files": jackpot(2), "overrides": {}, "locked": False})
    task = store.create_task("magnet:" + "d" * 40, "/115/infuse/电视剧", {"kind": "magnet", "title": "中头奖还是要上班",
                             "naming": naming, "pipeline": 2})
    store.update_task(task["id"], "naming_review", resume_state="naming_planning")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        sign_in(page, base, setup=True)
        page.get_by_role("button", name="入库任务", exact=True).click()
        page.get_by_role("button", name="查看详情", exact=True).click()
        expect(page.get_by_text("已排除 4 个广告、样片或未选文件，保留在下载暂存目录。", exact=True)).to_be_visible()
        expect(page.get_by_text("→ 中头奖还是要上班 (2024) [tmdbid=99]/Season 01/中头奖还是要上班 S01E01.mkv", exact=True)).to_be_visible()
        page.get_by_text("已排除 4 个文件，保留在暂存目录", exact=True).click()
        row = page.locator(".naming-excluded .naming-file").filter(has_text=f"{JACKPOT}/{AD}.MP4")
        expect(row.get_by_text("广告：", exact=False)).to_be_visible()
        expect(page.locator(".naming-excluded .naming-file").filter(has_text="更多资源.url").get_by_role("button")).to_have_count(0)
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        page.screenshot(path=str(tmp_path / "excluded-mobile.png"), full_page=True)
        row.get_by_role("button", name="改为入库", exact=True).click()
        expect(page.get_by_text("已排除 3 个文件，保留在暂存目录", exact=True)).to_be_visible()
        assert any(item["source"].endswith(f"{AD}.MP4") for item in store.task(task["id"])["payload"]["naming"]["plan"])
        assert not errors
        browser.close()
