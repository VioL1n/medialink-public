import asyncio
import copy
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from medialink.app import NamingPreviewCorrection, create_app
from medialink.core import Problem, Store, resource_key
from medialink.magnets import MagnetPreviews, preview_snapshot
from medialink.naming import bind_confirmed_preview, file_id
from medialink.naming_previews import NamingPreviews
from test_magnet_selection import TREE, create_worker, selection
from test_medialink import MAGNET, MemoryVault, authenticate, drive, new_worker
from test_naming import MOVIE, TV, enable_rename

pytest_plugins = ['test_medialink']
SHARE = 'https://115.com/s/testshare'


def service(store, files=None):
    store.configure({'llm_enabled': True})
    vault, magnets = MemoryVault(), MagnetPreviews(store)
    p115 = SimpleNamespace(share_entries=AsyncMock(return_value=[{'id': 'folder', 'name': 'S01', 'is_dir': True}]),
                           share_tree=AsyncMock(return_value=files or [{'path': 'S01/Show 02.mkv', 'size': 12}]))
    tmdb = SimpleNamespace(detail=AsyncMock(return_value=TV))
    llm = SimpleNamespace(suggest=AsyncMock(side_effect=lambda naming, candidates: {
        item['id']: {'episode_number': 2, 'reason': '文件编号02'} for item in candidates}))
    return NamingPreviews(store, vault, magnets, p115, tmdb, llm)


def request(link=SHARE, **extra):
    return {'link': link, 'password': 'private-code', 'selected_ids': ['folder'],
            'media': {'type': 'tv', 'id': TV['id'], 'season_number': 1}, 'resolution': None, **extra}


async def prepared(svc, data):
    draft = await svc.create(data)
    await svc.jobs[draft['preview_id']]
    return svc.get(draft['preview_id'])


async def test_share_preview_readonly_correction_confirmation_and_secret_storage(store):
    svc = service(store)
    data = request()
    draft = await prepared(svc, data)
    assert draft['status'] == 'ready' and not store.tasks()
    svc.p115.share_tree.assert_awaited_once_with(SHARE, 'private-code', svc.p115.share_entries.return_value)
    public = svc.public(draft)
    assert public['naming']['files'][0]['target'] == 'S01/绝命毒师 S01E02.mkv'
    with store.connect() as db:
        saved = str([dict(row) for row in db.execute('SELECT * FROM metadata_cache')])
    assert SHARE not in saved and 'private-code' not in saved
    reference = {'revision': draft['revision'], 'files': [{'id': file_id('S01/Show 02.mkv'), 'episode_number': 3}]}
    edited = svc.correct(draft['preview_id'], NamingPreviewCorrection(**reference))
    assert edited['revision'] > draft['revision'] and edited['naming']['plan'][0]['episode_number'] == 3
    with pytest.raises(Problem, match='更新'):
        svc.correct(draft['preview_id'], NamingPreviewCorrection(**reference))
    naming, selected = svc.confirm(edited['preview_id'], edited['revision'], {**data, 'auto_naming': True})
    assert naming['preview']['confirmed'] and not naming['review_required'] and selected is None
    assert naming['preview']['targets']['S01/Show 02.mkv'] == 'S01/绝命毒师 S01E03.mkv'
    assert svc.llm.suggest.await_count == 1


@pytest.mark.parametrize('changed', [
    {'selected_ids': []}, {'password': 'other'}, {'media': {'type': 'tv', 'id': TV['id'], 'season_number': 2}},
    {'resolution': '4k'}, {'link': 'https://115.com/s/other'}, {'auto_naming': False},
])
async def test_confirmation_rejects_changed_inputs(store, changed):
    svc = service(store)
    data = request()
    draft = await prepared(svc, data)
    with pytest.raises(Problem, match='不一致'):
        svc.confirm(draft['preview_id'], draft['revision'], {**data, 'auto_naming': True, **changed})


async def test_invalid_revision_unready_errors_expiry_and_restart(store):
    svc = service(store)
    draft = await prepared(svc, request())
    with pytest.raises(Problem, match='更新'):
        svc.confirm(draft['preview_id'], draft['revision'] - 1, {**request(), 'auto_naming': True})
    store.cache_metadata(svc.key(draft['preview_id']), draft, -1)
    with pytest.raises(Problem, match='过期'):
        svc.get(draft['preview_id'])
    draft = await prepared(svc, request())
    draft['status'] = 'analyzing'
    svc.save(draft)
    restarted = service(Store(store.path))
    assert restarted.get(draft['preview_id'])['status'] == 'failed'
    assert '中断' in restarted.get(draft['preview_id'])['error']
    restarted.llm.suggest.assert_not_awaited()
    draft['status'] = 'ready'
    draft['naming']['errors'] = ['conflict']
    svc.save(draft)
    with pytest.raises(Problem):
        svc.confirm(draft['preview_id'], draft['revision'], {**request(), 'auto_naming': True})


@pytest.mark.parametrize('action', ['delete', 'correct', 'expire'])
async def test_cancel_correct_or_expire_discards_late_model_result(store, action):
    svc = service(store)
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(naming, candidates):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {item['id']: {'episode_number': 9} for item in candidates}
    svc.llm.suggest = delayed
    draft = await svc.create(request())
    identifier = draft['preview_id']
    job = svc.jobs[identifier]
    await asyncio.wait_for(entered.wait(), 2)
    if action == 'delete':
        svc.delete(identifier)
    elif action == 'expire':
        store.cache_metadata(svc.key(identifier), svc.get(identifier), -1)
    else:
        current = svc.get(identifier)
        svc.correct(identifier, NamingPreviewCorrection(revision=current['revision'], files=[
            {'id': file_id('S01/Show 02.mkv'), 'episode_number': 4}]))
    release.set()
    await job
    if action == 'correct':
        assert svc.get(identifier)['naming']['plan'][0]['episode_number'] == 4
        assert not svc.get(identifier)['naming'].get('suggestions')
    else:
        assert svc.current(identifier) is None
    assert not store.tasks()


async def test_reanalysis_is_explicit_and_disabled_configuration_is_rejected(store):
    svc = service(store)
    draft = await prepared(svc, request())
    svc.llm.suggest.side_effect = Problem('大模型返回信息无效（JSON 格式）', 502)
    svc.reanalyze(draft['preview_id'])
    await svc.jobs[draft['preview_id']]
    assert svc.get(draft['preview_id'])['status'] == 'failed'
    assert svc.llm.suggest.await_count == 2
    store.configure({'llm_enabled': False})
    with pytest.raises(Problem, match='启用'):
        svc.reanalyze(draft['preview_id'])


@pytest.mark.parametrize('strip', [False, True])
async def test_magnet_selected_preview_auto_ingest_preserves_unselected(store, monkeypatch, strip):
    worker, task, ol = create_worker(store, strip=strip)
    svc = service(store)
    store.cache_metadata(svc.magnets.cache_key(resource_key(MAGNET)), preview_snapshot('a' * 40, '合集', TREE), 1800)
    data = request(MAGNET, selected_ids=[], media={'type': 'tv', 'id': TV['id']},
                   magnet_selection={k: selection()[k] for k in ('preview_id', 'selected_ids')})
    draft = await prepared(svc, data)
    svc.llm.suggest.assert_not_awaited()
    naming, chosen = svc.confirm(draft['preview_id'], draft['revision'], {**data, 'auto_naming': True})
    worker.llm.suggest = AsyncMock(side_effect=AssertionError('must not repeat model analysis'))
    task = worker.update(task, naming=naming, magnet_selection=chosen, pipeline=2)
    result = await drive(worker, task, 'scanning', iterations=100)
    assert result['state'] == 'scanning' and not result['payload']['naming']['review_required']
    assert result['payload']['retained_count'] == 2 and ol.offline_calls == 1
    # The whole torrent is downloaded; unselected files stay behind in the download staging folder.
    assert {item['size'] for item in await worker.file_manifest(result['payload']['staging'])} == {10, 200}
    placed = await worker.file_manifest('/115/infuse/绝命毒师 [tmdbid=1396]')
    assert {item['path'] for item in placed} == {'Season 01/绝命毒师 S01E01.mkv', 'Season 01/绝命毒师 S01E01.zh.srt',
                                                 'Season 02/绝命毒师 S02E01.mkv'}
    worker.llm.suggest.assert_not_awaited()
    svc.llm.suggest.assert_not_awaited()


@pytest.mark.parametrize('change', ['size', 'extra', 'missing', 'ambiguous', 'name', 'directory'])
async def test_download_mismatch_or_rename_conflict_cannot_use_approval(store, change):
    svc = service(store)
    draft = await prepared(svc, request())
    naming, _ = svc.confirm(draft['preview_id'], draft['revision'], {**request(), 'auto_naming': True})
    naming['files'] = copy.deepcopy(naming['files'])
    if change == 'size':
        naming['files'][0]['size'] += 1
    elif change == 'extra':
        naming['files'].append({'path': 'new.txt', 'size': 1})
    elif change == 'missing':
        naming['files'] = []
    elif change == 'ambiguous':
        naming['files'].append(copy.deepcopy(naming['files'][0]))
    elif change == 'name':
        naming['media']['chinese_title'] = '新版片名'
    else:
        naming['directories'] = ['S01/绝命毒师 S01E02.mkv']
    result, approved = bind_confirmed_preview(naming)
    assert not approved and result['review_required'] and result['errors']
    assert not result['preview']['confirmed']


def wait_preview(client, identifier):
    for _ in range(100):
        draft = client.get('/api/v1/naming/previews/' + identifier).json()
        if draft['status'] not in {'preparing', 'analyzing'}:
            return draft
        time.sleep(0.01)
    raise AssertionError('preview timed out')


def test_preview_api_and_task_submission_are_atomic_and_legacy_compatible(store):
    vault = MemoryVault()
    app = create_app(store, vault, start_workers=False)
    store.configure({'llm_enabled': True})
    app.state.p115.share_entries = AsyncMock(return_value=[{'id': 'folder', 'name': 'S01', 'is_dir': True}])
    app.state.p115.share_tree = AsyncMock(return_value=[{'path': 'S01/Show 02.mkv', 'size': 12}])
    app.state.tmdb.detail = AsyncMock(return_value=TV)
    app.state.llm.suggest = AsyncMock(return_value={file_id('S01/Show 02.mkv'): {'episode_number': 2}})
    app.state.openlist.get = AsyncMock(return_value={'is_dir': True})
    data = request()
    with TestClient(app) as client:
        authenticate(client)
        created = client.post('/api/v1/naming/previews', json=data)
        assert created.status_code == 200 and created.json()['status'] == 'preparing'
        draft = wait_preview(client, created.json()['preview_id'])
        assert not store.tasks()
        reference = {k: draft[k] for k in ('preview_id', 'revision')}
        body = {**data, 'target': '/115/infuse', 'auto_naming': True, 'naming_preview': reference}
        assert client.post('/api/v1/tasks', json={**body, 'resolution': '4K'}).status_code == 409
        assert not store.tasks()
        result = client.post('/api/v1/tasks', json=body)
        assert result.status_code == 200
        task = store.task(result.json()['id'])
        assert task['payload']['naming']['preview']['confirmed']
        assert client.post('/api/v1/tasks', json=body).json()['id'] == task['id']
        saved = copy.deepcopy(task['payload']['naming'])
        saved['preview']['confirmed'] = False
        store.update_task(task['id'], naming=saved)
        assert client.post('/api/v1/tasks', json=body).status_code == 409
        store.update_task(task['id'], naming=task['payload']['naming'])
        other = wait_preview(client, client.post('/api/v1/naming/previews', json=data).json()['preview_id'])
        assert client.post('/api/v1/tasks', json={**body, 'naming_preview': {k: other[k] for k in reference}}).status_code == 409
        assert len(store.tasks()) == 1
        assert client.delete('/api/v1/naming/previews/' + draft['preview_id']).status_code == 204
        assert client.get('/api/v1/naming/previews/' + draft['preview_id']).status_code == 409
        assert store.task(task['id'])['payload']['naming'] == task['payload']['naming']
        assert client.post('/api/v1/tasks', json={**data, 'target': '/115/infuse/old', 'auto_naming': False}).status_code == 200
        client.post('/api/v1/auth/logout')
        assert client.post('/api/v1/naming/previews', json=data).status_code == 401


async def test_root_mapping_remaps_model_and_manual_ids(store):
    source = '合集/S01/Show 02.mkv'
    svc = service(store, [{'path': source, 'size': 12}])
    draft = await prepared(svc, request())
    draft['root'] = '合集'
    svc.save(draft)
    draft = svc.correct(draft['preview_id'], NamingPreviewCorrection(revision=draft['revision'], files=[
        {'id': file_id(source), 'episode_number': 3}]))
    naming, _ = svc.confirm(draft['preview_id'], draft['revision'], {**request(), 'auto_naming': True})
    naming['files'] = [{'path': 'S01/Show 02.mkv', 'size': 12, 'identity': {'id': 'cloud-id'}}]
    mapped, approved = bind_confirmed_preview(naming)
    assert approved and mapped['plan'][0]['episode_number'] == 3
    assert mapped['plan'][0]['identity'] == {'id': 'cloud-id'}
    assert file_id(source) not in mapped['overrides']
    assert mapped['overrides'][file_id('S01/Show 02.mkv')]['episode_number'] == 3


@pytest.mark.parametrize('kind', ['magnet', 'share'])
async def test_once_confirmed_model_plan_reaches_emby_without_second_review(store, monkeypatch, kind):
    worker, task, ol, p115 = new_worker(store, kind)
    enable_rename(ol)
    svc = service(store, [{'path': 'open-movie.mkv', 'size': 1234}])
    svc.tmdb.detail.return_value = MOVIE
    svc.p115.share_entries.return_value = [{'id': '123', 'name': 'open-movie.mkv', 'is_dir': False, 'size': 1234}]
    svc.magnets.preview = AsyncMock(return_value=preview_snapshot('a' * 40, None, [{'path': 'open-movie.mkv', 'size': 1234}]))
    svc.llm.suggest = AsyncMock(return_value={file_id('open-movie.mkv'): {'resolution': '1080P', 'reason': '资源标注'}})
    data = request(MAGNET if kind == 'magnet' else SHARE, selected_ids=[] if kind == 'magnet' else ['123'], media={'type': 'movie', 'id': MOVIE['id']})
    draft = await prepared(svc, data)
    naming, chosen = svc.confirm(draft['preview_id'], draft['revision'], {**data, 'auto_naming': True})
    task = worker.update(task, naming=naming, magnet_selection=chosen, selected_ids=data['selected_ids'], pipeline=2)
    notify = AsyncMock()
    monkeypatch.setattr('medialink.worker.refresh_emby', notify)
    worker.llm.suggest = AsyncMock(side_effect=AssertionError('second model call'))
    result = await drive(worker, task, 'scanning')
    assert result['state'] == 'scanning'
    output = Path(store.settings()['strm_local']) / 'infuse/星际穿越 [tmdbid=157336]/Interstellar - 1080P.strm'
    output.parent.mkdir(parents=True)
    output.write_text('confirmed video')
    assert (await drive(worker, result))['state'] == 'completed'
    worker.llm.suggest.assert_not_awaited()
    svc.llm.suggest.assert_awaited_once()
    notify.assert_awaited_once()
    assert ol.offline_calls == (1 if kind == 'magnet' else 0)
    assert p115.calls == (1 if kind == 'share' else 0)


@pytest.mark.parametrize('media,source,resolution,target', [
    (TV, 'Season 1/Show 02.mkv', None, '绝命毒师 S01E02.mkv'),
    (MOVIE, '4k/movie.mkv', '1080P', 'Interstellar - 2160P.mkv'),
])
async def test_omitted_root_preserves_known_directory_context(store, media, source, resolution, target):
    svc = service(store, [{'path': source, 'size': 12}])
    svc.tmdb.detail.return_value = media
    data = request(media={'type': media['type'], 'id': media['id']}, resolution=resolution)
    draft = await prepared(svc, data)
    draft['root'] = source.split('/')[0]
    svc.save(draft)
    naming, _ = svc.confirm(draft['preview_id'], draft['revision'], {**data, 'auto_naming': True})
    naming['files'] = [{'path': source.split('/', 1)[1], 'size': 12}]
    mapped, approved = bind_confirmed_preview(naming)
    assert approved and mapped['plan'][0]['target'] == target


def test_submit_rechecks_revision_after_network_await(store):
    app = create_app(store, MemoryVault(), start_workers=False)
    store.configure({'llm_enabled': True})
    app.state.p115.share_entries = AsyncMock(return_value=[{'id': 'folder', 'name': 'S01', 'is_dir': True}])
    app.state.p115.share_tree = AsyncMock(return_value=[{'path': 'S01/Show.S01E02.mkv', 'size': 12}])
    app.state.tmdb.detail = AsyncMock(return_value=TV)
    with TestClient(app) as client:
        authenticate(client)
        draft = wait_preview(client, client.post('/api/v1/naming/previews', json=request()).json()['preview_id'])
        async def changed_target_check(target):
            app.state.naming_previews.delete(draft['preview_id'])
            return {'is_dir': True}
        app.state.openlist.get = changed_target_check
        result = client.post('/api/v1/tasks', json={**request(), 'target': '/115/infuse', 'auto_naming': True,
                             'naming_preview': {k: draft[k] for k in ('preview_id', 'revision')}})
        assert result.status_code == 409 and not store.tasks()
