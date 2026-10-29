import os
from unittest.mock import AsyncMock

import pytest
from medialink.naming import build_plan, file_id
from playwright.sync_api import expect, sync_playwright
from test_naming import TV
from test_settings_browser import sign_in

pytest_plugins = ['test_settings_browser', 'test_medialink']
pytestmark = pytest.mark.skipif(os.environ.get('MEDIALINK_BROWSER_TESTS') != '1', reason='requires Chromium and isolated frontend build')


def test_llm_settings_save_check_preserves_other_drafts_and_mobile_layout(browser_app):
    base, app, store, tmp_path = browser_app
    app.state.llm.check = AsyncMock(return_value={'ok': True, 'message': '已保存大模型配置检测成功'})
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 1000})
        errors, patches = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: patches.append(request.post_data_json) if request.method == 'PATCH' and request.url.endswith('/api/v1/settings') else None)
        sign_in(page, base, setup=True)
        page.get_by_role('tab', name='目录与网络').click()
        page.get_by_label('代理地址', exact=True).fill('socks5://127.0.0.1:7890')
        page.get_by_role('tab', name='服务连接').click()
        expect(page.get_by_label('启用大模型辅助命名', exact=True)).not_to_be_checked()
        expect(page.get_by_label('大模型使用应用代理', exact=True)).not_to_be_checked()
        page.get_by_label('大模型 Base URL', exact=True).fill('https://example.test/compatible-mode/v1/')
        page.get_by_label('大模型名称', exact=True).fill('test-model')
        page.get_by_label('大模型 API Key', exact=True).fill('browser-test-llm-secret')
        page.get_by_label('启用大模型辅助命名', exact=True).check()
        page.get_by_role('button', name='保存大模型配置', exact=True).click()
        expect(page.get_by_text('大模型配置已保存', exact=True)).to_be_visible()
        assert store.settings()['llm_enabled'] and store.settings()['proxy'] == ''
        assert set(patches[-1]) == {'llm_enabled', 'llm_base_url', 'llm_model', 'llm_use_proxy', 'llm_api_key'}
        expect(page.get_by_label('大模型 API Key', exact=True)).to_have_value('')
        expect(page.get_by_label('大模型 Base URL', exact=True)).to_have_value('https://example.test/compatible-mode/v1')
        # Detection uses saved values and cannot overwrite a model draft or unrelated proxy draft.
        page.get_by_label('大模型名称', exact=True).fill('draft-model')
        page.get_by_role('button', name='检测大模型', exact=True).click()
        expect(page.get_by_text('已保存大模型配置检测成功', exact=True)).to_be_visible()
        assert store.settings()['llm_model'] == 'test-model' and len(patches) == 1
        expect(page.get_by_label('大模型名称', exact=True)).to_have_value('draft-model')
        page.screenshot(path=str(tmp_path / 'llm-settings-desktop.png'), full_page=True)
        page.get_by_role('tab', name='目录与网络').click()
        expect(page.get_by_label('代理地址', exact=True)).to_have_value('socks5://127.0.0.1:7890')
        page.get_by_role('tab', name='服务连接').click()
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(tmp_path / 'llm-settings-mobile.png'), full_page=True)
        page.get_by_role('button', name='清除大模型密钥', exact=True).click()
        expect(page.get_by_text('大模型密钥已清除，辅助命名已关闭', exact=True)).to_be_visible()
        assert not store.settings()['llm_enabled']
        expect(page.get_by_label('大模型名称', exact=True)).to_have_value('draft-model')
        assert not errors
        browser.close()


def test_llm_preview_poll_drafts_save_confirmation_and_mobile_layout(browser_app):
    base, app, store, tmp_path = browser_app
    store.configure({'llm_enabled': True})
    naming = build_plan({'version': 1, 'media': TV, 'season_number': 1,
                         'files': [{'path': 'Show 02.mkv', 'size': 12}], 'overrides': {}, 'locked': False,
                         'suggestions': {file_id('Show 02.mkv'): {'episode_number': 2, 'reason': '文件编号02'}},
                         'review_required': True, 'revision': 2,
                         'llm': {'status': 'completed', 'message': '识别完成，请核对名称后确认重命名'}})
    task = store.create_task('magnet:' + 'd' * 40, '/115/infuse', {'kind': 'magnet', 'title': '模型预览剧集', 'naming': naming, 'staging': '/115/staging/preview'})
    store.update_task(task['id'], 'naming_review', resume_state='naming_planning')
    app.state.worker.file_manifest = AsyncMock(return_value=naming['files'])
    app.state.worker.manifest_directories = []
    app.state.openlist.rename = AsyncMock()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        sign_in(page, base, setup=True)
        page.get_by_role('button', name='入库任务', exact=True).click()
        expect(page.get_by_text('等待确认命名', exact=True)).to_be_visible()
        page.get_by_role('button', name='查看详情', exact=True).click()
        expect(page.get_by_text('→ 绝命毒师 S01E02.mkv', exact=True)).to_be_visible()
        expect(page.get_by_text('模型依据：文件编号02', exact=True)).to_be_visible()
        confirm = page.get_by_role('button', name='确认并重命名', exact=True)
        expect(confirm).to_be_enabled()
        page.get_by_label('Show 02.mkv 集号', exact=True).fill('3')
        expect(confirm).to_be_disabled()
        with page.expect_response(lambda response: response.url.endswith('/api/v1/tasks/' + task['id']) and response.request.method == 'GET', timeout=8000):
            pass
        expect(page.get_by_label('Show 02.mkv 集号', exact=True)).to_have_value('3')
        expect(confirm).to_be_disabled()
        page.get_by_role('button', name='保存命名预览', exact=True).click()
        expect(page.get_by_text('→ 绝命毒师 S01E03.mkv', exact=True)).to_be_visible()
        expect(confirm).to_be_enabled()
        assert store.task(task['id'])['state'] == 'naming_review'
        app.state.openlist.rename.assert_not_awaited()
        page.screenshot(path=str(tmp_path / 'llm-naming-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(tmp_path / 'llm-naming-mobile.png'), full_page=True)
        confirm.click()
        expect(page.locator('.modal .task-status')).to_have_text('重命名并核对文件')
        assert store.task(task['id'])['state'] == 'renaming'
        assert not store.task(task['id'])['payload']['naming']['review_required']
        expect(page.get_by_role('button', name='确认并重命名', exact=True)).to_have_count(0)
        assert not errors
        browser.close()
