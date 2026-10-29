import asyncio
import os
import socket
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import uvicorn
from medialink.app import create_app
from medialink.core import Store
from medialink.magnets import preview_snapshot
from playwright.sync_api import expect, sync_playwright
from test_medialink import MemoryVault

pytestmark = pytest.mark.skipif(os.environ.get('MEDIALINK_BROWSER_TESTS') != '1', reason='requires installed Chromium and frontend build')
OLD_PASSWORD = 'browser-password-123'
NEW_PASSWORD = 'browser-password-456'


@pytest.fixture
def browser_app(tmp_path):
    dist = Path(os.environ.get('MEDIALINK_TEST_FRONTEND_DIST', str(Path(__file__).resolve().parents[1] / 'frontend/dist')))
    if not (dist / 'index.html').exists():
        pytest.fail('Build the frontend before running browser tests')
    store, vault = Store(tmp_path / 'state.db'), MemoryVault()
    app = create_app(store, vault, start_workers=False, frontend_dist=dist)
    app.state.tmdb.state = AsyncMock(return_value={'tmdb_configured': True, 'tmdb_auth_type': 'token'})
    app.state.tmdb.search = AsyncMock(return_value={'items': [], 'updated': 1, 'stale': False})
    app.state.tmdb.detail = AsyncMock(return_value={
        'id': 100, 'type': 'movie', 'title': 'Movie', 'english_title': 'Movie',
        'year': 2020, 'votes': 0, 'overview': 'Test movie', 'updated': 1, 'stale': False,
    })
    app.state.tmdb.check = AsyncMock(return_value={'ok': True, 'message': '已保存配置检测成功'})
    async def magnet_preview(link, key):
        return preview_snapshot(key.split(":", 1)[1], None, [{"path": "movie.1080p.mkv", "size": 1234}])
    app.state.magnets.resolve = AsyncMock(side_effect=magnet_preview)
    app.state.nyaa.search = AsyncMock(return_value=[])
    app.state.telegram.search = AsyncMock(return_value=[])
    app.state.knaben.search = AsyncMock(return_value=[])
    app.state.limetorrents.search = AsyncMock(return_value=[])
    app.state.home.local.home = AsyncMock(return_value={"origin": "local", "featured": [], "sections": [], "errors": []})
    app.state.tmdb.image = AsyncMock(return_value=(b"", "image/png"))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    base = f'http://127.0.0.1:{listener.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield base, app, store, tmp_path
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()


def sign_in(page, base, setup=False):
    page.goto(base+'/#/settings')
    page.get_by_label('个人访问密码').fill(OLD_PASSWORD)
    page.get_by_role('button', name='设置密码并进入' if setup else '登录 MediaLink', exact=True).click()
    expect(page.get_by_role('heading', name='默认搜索源', exact=True)).to_be_visible()


def test_settings_drafts_sources_and_mobile_layout(browser_app):
    base, app, store, tmp_path = browser_app
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={'width': 1280, 'height': 900})
        page = context.new_page()
        errors, searches, patches = [], [], []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def record(request):
            if request.method == 'POST' and request.url.endswith('/api/v1/search'):
                searches.append(request.post_data_json)
            if request.method == 'PATCH' and request.url.endswith('/api/v1/settings'):
                patches.append(request.post_data_json)

        page.on('request', record)
        sign_in(page, base, setup=True)
        for name in ['Knaben', 'LimeTorrents']:
            page.get_by_label('启用 '+name, exact=True).uncheck()
        page.get_by_label('普通搜索默认开启 TMDB 片名辅助').uncheck()
        page.get_by_role('tab', name='目录与网络').click()
        page.get_by_label('代理地址', exact=True).fill('socks5://127.0.0.1:7890')
        page.get_by_role('tab', name='服务连接').click()
        page.get_by_text('个人账号 API 模式（可选）', exact=True).click()
        page.get_by_label('API ID', exact=True).fill('123456')
        page.get_by_label('API Hash', exact=True).fill('draft-hash')
        page.get_by_role('tab', name='搜索偏好').click()
        page.get_by_role('button', name='保存搜索偏好').click()
        expect(page.locator('#settings-panel-search').get_by_role('status')).to_have_text('设置已保存')
        assert store.settings()['enabled_search_sources'] == ['nyaa', 'telegram']
        assert store.settings()['proxy'] == '' and store.settings()['telegram_api_id'] == ''
        assert set(patches[-1]) == {'enabled_search_sources', 'default_use_tmdb', 'knaben_url', 'limetorrents_url', 'nyaa_url', 'mikan_url', 'yts_url', 'eztv_url'}
        page.screenshot(path=str(tmp_path/'settings-desktop.png'), full_page=True)
        page.get_by_role('tab', name='目录与网络').click()
        expect(page.get_by_label('代理地址', exact=True)).to_have_value('socks5://127.0.0.1:7890')
        page.get_by_role('tab', name='服务连接').click()
        expect(page.get_by_label('API ID', exact=True)).to_have_value('123456')
        expect(page.get_by_label('API Hash', exact=True)).to_have_value('draft-hash')
        # Connection checks must not save other drafts.
        page.get_by_role('button', name='检测 TMDB', exact=True).click()
        expect(page.get_by_text('已保存配置检测成功', exact=True)).to_be_visible()
        assert len(patches) == 1
        assert store.settings()['proxy'] == ''
        page.get_by_role('button', name='资源搜索', exact=True).click()
        expect(page.locator('.source-toggles label')).to_have_count(2)
        expect(page.get_by_label('TMDB 片名辅助', exact=True)).not_to_be_checked()
        expect(page.locator('.source-toggles input:checked')).to_have_count(2)
        page.locator('.source-toggles label').filter(has_text='Telegram 频道').locator('input').uncheck()
        page.get_by_label('影视资源关键词').fill('Movie')
        page.get_by_role('button', name='搜索资源', exact=True).click()
        expect(page.get_by_role('heading', name='暂时没有匹配资源')).to_be_visible()
        assert searches[-1]['sources'] == ['nyaa'] and searches[-1]['use_tmdb'] is False
        expect(page.get_by_label('资源来源').locator('option')).to_have_count(3)
        assert store.settings()['enabled_search_sources'] == ['nyaa', 'telegram']
        page.goto(base+'/#/media/movie/100')
        expect(page.get_by_role('heading', name='Movie', exact=True)).to_be_visible()
        expect(page.get_by_role('heading', name='暂时没有匹配资源')).to_be_visible()
        assert searches[-1]['sources'] == ['nyaa', 'telegram'] and searches[-1]['media'] == {'type':'movie', 'id':100}
        page.get_by_role('button', name='设置', exact=True).click()
        page.get_by_label('启用 Nyaa', exact=True).uncheck()
        page.get_by_label('启用 Telegram 频道', exact=True).uncheck()
        page.get_by_role('button', name='保存搜索偏好').click()
        expect(page.locator('#settings-panel-search').get_by_role('status')).to_have_text('设置已保存')
        before = len(searches)
        page.goto(base+'/#/media/movie/100')
        expect(page.get_by_role('heading', name='尚未启用搜索源')).to_be_visible()
        assert len(searches) == before
        page.get_by_role('button', name='资源搜索', exact=True).click()
        expect(page.get_by_role('heading', name='尚未启用搜索源')).to_be_visible()
        page.get_by_role('button', name='粘贴链接', exact=True).click()
        expect(page.get_by_role('heading', name='粘贴资源链接')).to_be_visible()
        page.locator('.modal-close').click()
        page.get_by_role('button', name='设置', exact=True).click()
        page.set_viewport_size({'width':390, 'height':844})
        for tab in ['搜索偏好', '服务连接', '目录与网络', '安全与访问']:
            page.get_by_role('tab', name=tab).click()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=str(tmp_path/('settings-mobile-'+str(['搜索偏好', '服务连接', '目录与网络', '安全与访问'].index(tab))+'.png')), full_page=True)
        assert not errors
        context.close()
        browser.close()


def test_password_form_and_sse_logout_other_browser(browser_app):
    base, app, store, _ = browser_app

    async def slow_search(query):
        await asyncio.sleep(30)
        return []

    app.state.nyaa.search.side_effect = slow_search
    with sync_playwright() as p:
        browser = p.chromium.launch()
        first = browser.new_context(viewport={'width':1280, 'height':900})
        second = browser.new_context(viewport={'width':390, 'height':844})
        page, other = first.new_page(), second.new_page()
        sign_in(page, base, setup=True)
        store.configure({'enabled_search_sources':['nyaa'], 'default_use_tmdb':False})
        other.add_init_script('''const Original=window.EventSource;
          window.EventSource=class extends Original { constructor(...args) {super(...args); this.addEventListener('auth_required',()=>window.revokedBySSE=true)} };''')
        sign_in(other, base)
        other.get_by_role('button', name='资源搜索', exact=True).click()
        other.get_by_label('影视资源关键词').fill('Movie')
        other.get_by_role('button', name='搜索资源', exact=True).click()
        expect(other.get_by_role('heading', name='正在汇聚资源…')).to_be_visible()
        page.get_by_role('tab', name='安全与访问').click()
        page.get_by_label('当前密码', exact=True).fill('wrong-password')
        page.get_by_label('新密码', exact=True).fill(NEW_PASSWORD)
        page.get_by_label('确认新密码', exact=True).fill(NEW_PASSWORD+'x')
        page.get_by_role('button', name='修改密码并重新登录').click()
        expect(page.get_by_text('两次输入的新密码不一致', exact=True)).to_be_visible()
        page.get_by_label('确认新密码', exact=True).fill(NEW_PASSWORD)
        page.get_by_role('button', name='修改密码并重新登录').click()
        expect(page.get_by_text('当前密码不正确', exact=True)).to_be_visible()
        expect(page.get_by_role('heading', name='修改访问密码', exact=True)).to_be_visible()
        page.get_by_label('当前密码', exact=True).fill(OLD_PASSWORD)
        page.get_by_role('button', name='修改密码并重新登录').click()
        expect(page.get_by_text('密码已修改，请使用新密码重新登录', exact=True)).to_be_visible()
        other.wait_for_function('window.revokedBySSE === true', timeout=3000)
        expect(other.get_by_label('个人访问密码')).to_be_visible()
        expect(page.get_by_label('个人访问密码')).to_have_value('')
        page.get_by_label('个人访问密码').fill(OLD_PASSWORD)
        page.get_by_role('button', name='登录 MediaLink', exact=True).click()
        expect(page.get_by_text('密码不正确', exact=True)).to_be_visible()
        page.get_by_label('个人访问密码').fill(NEW_PASSWORD)
        page.get_by_role('button', name='登录 MediaLink', exact=True).click()
        expect(page.get_by_role('heading', name='默认搜索源', exact=True)).to_be_visible()
        first.close()
        second.close()
        browser.close()


def test_independent_home_empty_state_and_four_source_settings(browser_app):
    base, app, _, tmp_path = browser_app
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        sign_in(page, base, setup=True)
        page.goto(base + '/#/discover')
        expect(page.get_by_text('暂时没有新收录的资源，可在设置中配置 TMDB 并添加 Telegram 频道。', exact=True)).to_be_visible()
        assert app.state.home.local.home.await_count > 0
        page.get_by_role('button', name='设置', exact=True).click()
        expect(page.locator('.settings-source-option')).to_have_count(4)
        assert page.locator('.settings-source-option b').all_text_contents() == ['Knaben', 'LimeTorrents', 'Nyaa', 'Telegram 频道']
        assert page.get_by_label('首页数据来源', exact=True).count() == 0
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / 'four-sources-mobile.png'), full_page=True)
        browser.close()
