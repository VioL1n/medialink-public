import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from medialink.magnets import preview_snapshot
from playwright.sync_api import expect, sync_playwright
from test_medialink import MAGNET
from test_naming import TV
from test_naming_browser import configure
from test_settings_browser import sign_in

pytest_plugins = ['test_settings_browser', 'test_medialink']
pytestmark = pytest.mark.skipif(os.environ.get('MEDIALINK_BROWSER_TESTS') != '1', reason='requires Chromium and isolated build')
FILES = [{'path': '合集/S01/Show 02.mkv', 'size': 120},
         {'path': '合集/S01/Show 02.zh.srt', 'size': 20}, {'path': '合集/readme.txt', 'size': 10}]


@pytest.mark.parametrize('kind', ['magnet', 'share'])
def test_modal_preview_edit_invalidate_cancel_and_single_confirmation(browser_app, kind):
    base, app, store, tmp_path = browser_app
    configure(app, store)
    store.configure({'llm_enabled': True})
    tv = {**TV, 'votes': 0, 'overview': '测试资料', 'updated': 1, 'stale': False}
    app.state.tmdb.search = AsyncMock(return_value={'items': [tv], 'updated': 1, 'stale': False})
    app.state.tmdb.detail = AsyncMock(return_value=tv)
    app.state.magnets.resolve = AsyncMock(return_value=preview_snapshot('a' * 40, '合集', FILES))
    app.state.p115.share_entries = AsyncMock(return_value=[{'id': 'folder', 'name': '合集', 'is_dir': True, 'size': 0}])
    app.state.p115.share_tree = AsyncMock(return_value=FILES)
    app.state.p115.receive = AsyncMock()
    app.state.openlist.offline = AsyncMock()
    async def suggest(naming, candidates):
        await asyncio.sleep(0.3)
        return {item['id']: {'episode_number': 2, 'reason': '文件编号02'} for item in candidates}
    app.state.llm.suggest = AsyncMock(side_effect=suggest)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 1000})
        errors, submissions = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda req: submissions.append(req.post_data_json) if req.method == 'POST' and req.url.endswith('/api/v1/tasks') else None)
        sign_in(page, base, setup=True)
        page.get_by_role('button', name='资源搜索', exact=True).click()
        page.get_by_role('button', name='粘贴链接', exact=True).click()
        page.get_by_label('磁力链接或 115 分享').fill(MAGNET if kind == 'magnet' else 'https://115.com/s/testshare')
        page.get_by_role('button', name='选择入库目录', exact=True).click()
        expect(page.get_by_role('checkbox', name='自动规范命名', exact=True)).to_be_checked()
        page.get_by_label('TMDB 影片关键词').fill('绝命毒师')
        page.get_by_role('button', name='搜索 TMDB', exact=True).click()
        page.get_by_role('button', name='绝命毒师 · 年份未知 · 剧集', exact=True).click()
        if kind == 'share':
            page.get_by_role('button', name='预览分享并选择内容', exact=True).click()
            expect(page.locator('.share-list input:checked')).to_have_count(1)
        else:
            expect(page.get_by_role('status').filter(has_text='已选 2 / 3')).to_be_visible()
        page.get_by_role('button', name='大模型规范命名', exact=True).click()
        expect(page.get_by_role('button', name='确认入库', exact=True)).to_be_disabled()
        expect(page.get_by_text('→ 绝命毒师 [tmdbid=1396]/Season 01/绝命毒师 S01E02.mkv', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='确认入库', exact=True)).to_be_enabled()
        assert not store.tasks()
        app.state.openlist.offline.assert_not_awaited()
        app.state.p115.receive.assert_not_awaited()
        page.get_by_role('button', name='重新识别', exact=True).click()
        expect(page.get_by_role('button', name='确认入库', exact=True)).to_be_disabled()
        expect(page.get_by_role('button', name='确认入库', exact=True)).to_be_enabled()
        assert app.state.llm.suggest.await_count == 2
        # Input changes delete the trusted draft and clear its approval.
        with page.expect_response(lambda r: '/api/v1/naming/previews/' in r.url and r.request.method == 'DELETE'):
            page.get_by_label('所选季数', exact=True).fill('1')
        expect(page.get_by_role('button', name='大模型规范命名', exact=True)).to_be_visible()
        page.get_by_role('button', name='大模型规范命名', exact=True).click()
        expect(page.get_by_text('→ 绝命毒师 [tmdbid=1396]/Season 01/绝命毒师 S01E02.mkv', exact=True)).to_be_visible()
        page.get_by_label('合集/S01/Show 02.mkv 集号', exact=True).fill('3')
        expect(page.get_by_role('button', name='确认入库', exact=True)).to_be_disabled()
        page.get_by_role('button', name='保存命名预览', exact=True).click()
        expect(page.get_by_text('→ 绝命毒师 [tmdbid=1396]/Season 01/绝命毒师 S01E03.mkv', exact=True)).to_be_visible()
        expect(page.get_by_text('→ 绝命毒师 [tmdbid=1396]/Season 01/绝命毒师 S01E03.zh.srt', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='确认并重命名', exact=True)).not_to_be_visible()
        expect(page.get_by_role('button', name='确认入库', exact=True)).to_be_enabled()
        page.screenshot(path=str(tmp_path / ('preview-' + kind + '-desktop.png')), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(tmp_path / ('preview-' + kind + '-mobile.png')), full_page=True)
        page.get_by_role('button', name='确认入库', exact=True).click()
        expect(page.get_by_text('任务已添加，可在入库任务中查看', exact=True)).to_be_visible()
        assert len(submissions) == len(store.tasks()) == 1
        task = store.tasks()[0]
        assert task['payload']['naming']['preview']['confirmed']
        assert task['payload']['naming']['overrides']
        assert submissions[0]['naming_preview']['revision'] == task['payload']['naming']['preview']['revision']
        app.state.openlist.offline.assert_not_awaited()
        app.state.p115.receive.assert_not_awaited()
        assert not errors
        browser.close()


def test_close_dialog_cancels_even_a_late_create_response(browser_app):
    base, app, store, _ = browser_app
    configure(app, store)
    store.configure({'llm_enabled': True})
    tv = {**TV, 'votes': 0, 'overview': '测试资料', 'updated': 1, 'stale': False}
    app.state.tmdb.search = AsyncMock(return_value={'items': [tv], 'updated': 1, 'stale': False})
    app.state.tmdb.detail = AsyncMock(return_value=tv)
    app.state.magnets.resolve = AsyncMock(return_value=preview_snapshot('a' * 40, '合集', FILES))
    async def slow_model(*args):
        await asyncio.sleep(30)
        return {}
    app.state.llm.suggest = AsyncMock(side_effect=slow_model)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script('''const originalFetch=window.fetch;
          window.fetch=async (...args)=>{const response=await originalFetch(...args);
            if(args[0]==='/api/v1/naming/previews'&&args[1]?.method==='POST')await new Promise(resolve=>setTimeout(resolve,700));
            return response;}''')
        sign_in(page, base, setup=True)
        page.goto(base + '/#/media/tv/1396')
        page.get_by_role('button', name='入库 115', exact=True).click()
        page.get_by_role('checkbox', name='自动规范命名', exact=True).check()
        expect(page.get_by_role('button', name='大模型规范命名', exact=True)).to_be_enabled()
        with page.expect_response(lambda r: r.url.endswith('/api/v1/naming/previews') and r.request.method == 'POST'):
            page.get_by_role('button', name='大模型规范命名', exact=True).click()
        with page.expect_response(lambda r: '/api/v1/naming/previews/' in r.url and r.request.method == 'DELETE'):
            page.locator('.modal-close').click()
        assert not store.tasks()
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM metadata_cache WHERE key LIKE 'naming-preview:%'").fetchone()[0] == 0
        assert not errors
        browser.close()
