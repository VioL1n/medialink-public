import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from medialink.app import create_app
from medialink.core import Problem, Store
from medialink.llm import NamingLLM, validate_suggestions
from medialink.naming import build_plan, file_id, model_candidates
from medialink.worker import Worker
from test_medialink import MemoryVault, authenticate, drive, new_worker
from test_naming import MOVIE, TV, add_naming, enable_rename, plan

pytest_plugins = ['test_medialink']
API_KEY = 'test-llm-key-never-return'
URL = 'https://example.test/prefix/compatible-mode/v1'


def configure(store, vault):
    store.configure({'llm_enabled': True, 'llm_base_url': URL, 'llm_model': 'test-model'})
    vault.set('llm-api-key', API_KEY)


@pytest.fixture
def llm_client(store):
    vault = MemoryVault()
    app = create_app(store, vault, start_workers=False)
    with TestClient(app) as client:
        authenticate(client)
        yield client, store, vault


def test_llm_settings_defaults_save_normalize_preserve_clear_restart(llm_client):
    client, store, vault = llm_client
    settings = client.get('/api/v1/settings').json()
    assert settings['llm_enabled'] is False and settings['llm_use_proxy'] is False
    assert settings['llm_key_saved'] is False and settings['llm_configured'] is False
    body = {'llm_enabled': True, 'llm_base_url': '  ' + URL + '/  ', 'llm_model': ' test-model ', 'llm_api_key': API_KEY}
    result = client.patch('/api/v1/settings', json=body)
    assert result.status_code == 200
    assert API_KEY not in result.text and 'llm_api_key' not in result.json()
    assert result.json()['llm_base_url'] == URL
    assert result.json()['llm_model'] == 'test-model'
    assert result.json()['llm_configured'] and result.json()['llm_key_saved']
    assert vault.get('llm-api-key') == API_KEY
    with store.connect() as db:
        rows = db.execute('SELECT * FROM settings').fetchall()
    assert API_KEY not in str([dict(row) for row in rows])
    assert client.patch('/api/v1/settings', json={'llm_api_key': '', 'llm_model': 'new-model'}).status_code == 200
    assert vault.get('llm-api-key') == API_KEY
    with TestClient(create_app(Store(store.path), vault, start_workers=False)) as restarted:
        restarted.post('/api/v1/auth/login', json={'password': 'test-password-123'})
        assert restarted.get('/api/v1/settings').json()['llm_configured']
    result = client.patch('/api/v1/settings', json={'llm_api_key': None})
    assert result.status_code == 200 and result.json()['llm_enabled'] is False
    assert result.json()['llm_key_saved'] is False and result.json()['llm_configured'] is False
    assert vault.get('llm-api-key') is None


@pytest.mark.parametrize('body', [
    {'llm_enabled': True}, {'llm_enabled': 1}, {'llm_use_proxy': 'false'},
    {'llm_base_url': 'https://user:secret@example.test/v1'},
    {'llm_base_url': 'https://example.test/v1?api_key=secret'},
    {'llm_base_url': 'https://example.test/v1#secret'}, {'llm_base_url': 'ftp://example.test'},
    {'llm_base_url': 'https://example.test/v1/chat/completions'},
    {'llm_base_url': 'https://example.test:bad/v1'}, {'llm_base_url': 'https://example.test/a b'},
    {'llm_base_url': None}, {'llm_model': 'model with space'}, {'llm_model': []},
    {'llm_api_key': 'test\nsecret'}, {'llm_api_key': ['test-secret']}, {'llm_api_key': True},
])
def test_invalid_settings_are_atomic_and_do_not_echo_inputs(llm_client, body):
    client, store, vault = llm_client
    before = store.settings()
    result = client.patch('/api/v1/settings', json=body)
    assert result.status_code == 422 and 'secret' not in result.text
    assert store.settings() == before and vault.get('llm-api-key') is None


def test_failed_keychain_write_does_not_save_llm_settings(llm_client, monkeypatch):
    client, store, vault = llm_client
    before = store.settings()
    def fail(*args):
        raise Problem('无法保存到 macOS Keychain', 503)
    monkeypatch.setattr(vault, 'set', fail)
    response = client.patch('/api/v1/settings', json={'llm_api_key': API_KEY, 'llm_base_url': URL, 'llm_model': 'model'})
    assert response.status_code == 503 and API_KEY not in response.text
    assert store.settings() == before


def install_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    options = []
    def client(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handler), timeout=60)
    monkeypatch.setattr('medialink.llm.httpx.AsyncClient', client)
    return options


def response_for(files):
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'files': files})}}]}


@pytest.mark.parametrize('use_proxy', [False, True])
async def test_client_prefix_auth_minimal_payload_and_proxy(store, monkeypatch, use_proxy):
    vault = MemoryVault()
    configure(store, vault)
    store.configure({'proxy': 'socks5://127.0.0.1:7890', 'llm_use_proxy': use_proxy})
    naming = plan(['Season 1/Show 02.mkv'], TV)
    candidates = model_candidates(naming)
    assert candidates[0]['missing_fields'] == ['episode_number']
    def handler(request):
        assert str(request.url) == URL + '/chat/completions'
        assert request.headers['authorization'] == 'Bearer ' + API_KEY
        body = json.loads(request.content)
        assert set(body) == {'model', 'messages', 'stream', 'response_format'} and body['stream'] is False
        assert body['response_format'] == {'type': 'json_object'}
        example = json.loads(body['messages'][0]['content'].split('占位，能确定时填写实际值）：')[1])
        assert set(example['files'][0]) == {'id', 'episode_number', 'reason'}
        data = json.loads(body['messages'][1]['content'])
        assert set(data) == {'media', 'season_number', 'resolution', 'files'}
        assert 'size' not in data['files'][0] and 'identity' not in data['files'][0]
        return httpx.Response(200, json=response_for([{'id': candidates[0]['id'], 'episode_number': 2, 'reason': '文件编号02'}]))
    options = install_transport(monkeypatch, handler)
    suggestions = await NamingLLM(store, vault).suggest(naming, candidates)
    assert suggestions[candidates[0]['id']]['episode_number'] == 2
    assert options[0]['proxy'] == ('socks5://127.0.0.1:7890' if use_proxy else None)
    assert options[0]['trust_env'] is False and options[0]['follow_redirects'] is False


@pytest.mark.parametrize('status', [301, 400, 401, 403, 429, 500])
async def test_upstream_errors_do_not_leak_secrets(store, monkeypatch, status):
    vault = MemoryVault()
    configure(store, vault)
    install_transport(monkeypatch, lambda request: httpx.Response(status, text=API_KEY + ' upstream secret', headers={'location': 'https://other.test'}))
    with pytest.raises(Problem) as error:
        await NamingLLM(store, vault).suggest({}, [])
    assert API_KEY not in error.value.message and 'secret' not in error.value.message


async def test_timeout_and_malformed_envelopes(store, monkeypatch):
    vault = MemoryVault()
    configure(store, vault)
    def fail(request):
        raise httpx.ReadTimeout(API_KEY, request=request)
    install_transport(monkeypatch, fail)
    with pytest.raises(Problem, match='超时'):
        await NamingLLM(store, vault).suggest({}, [])


@pytest.mark.parametrize('envelope', [
    {}, {'choices': []}, {'choices': [{'message': {'content': None}}]},
    {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]},
    {'choices': [{'message': {'content': '{}', 'refusal': 'refused'}}]},
])
async def test_invalid_envelope_is_rejected(store, monkeypatch, envelope):
    vault = MemoryVault()
    configure(store, vault)
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=envelope))
    with pytest.raises(Problem):
        await NamingLLM(store, vault).suggest({}, [])


@pytest.mark.parametrize('payload', [
    'not JSON', '```json\n{}\n```', '{"files":[],"files":[]}', '{"files":[],"unknown":true}',
    {'files': [{'id': 'a' * 24, 'episode_number': 2}]},
    {'files': [{'id': file_id('Show 02.mkv'), 'episode_number': 0}]},
    {'files': [{'id': file_id('Show 02.mkv'), 'episode_number': 10000}]},
    {'files': [{'id': file_id('Show 02.mkv'), 'episode_number': True}]},
    {'files': [{'id': file_id('Show 02.mkv'), 'episode_number': '2x'}]},
    {'files': [{'id': file_id('Show 02.mkv'), 'season_number': 2}]},
    {'files': [{'id': file_id('Show 02.mkv'), 'target': '../unsafe.mkv'}]},
    {'files': [{'id': file_id('Show 02.mkv')}, {'id': file_id('Show 02.mkv')}]},
])
def test_suggestions_validate_shape_ids_ranges_and_missing_fields(payload):
    naming = plan(['Show 02.mkv'], TV, season_number=1)
    with pytest.raises(Problem):
        validate_suggestions(payload if isinstance(payload, str) else json.dumps(payload), model_candidates(naming))


@pytest.mark.parametrize('fenced', [False, True])
def test_compatible_response_discards_nulls_and_identical_known_echoes(fenced):
    candidates = model_candidates(plan(['Show 02.mkv'], TV, season_number=1))
    identifier = candidates[0]['id']
    content = json.dumps({'files': [{'id': identifier, 'episode_number': '02', 'season_number': '1',
                                   'resolution': None, 'reason': '编号02'}]})
    if fenced:
        content = '```json\n' + content + '\n```'
    assert validate_suggestions(content, candidates) == {identifier: {'episode_number': 2, 'reason': '编号02'}}


@pytest.mark.parametrize('quality,expected', [('1080p', '1080P'), ('4k', '2160P'), ('8K', '4320P')])
def test_quality_aliases(quality, expected):
    candidates = model_candidates(plan(['movie.mkv']))
    identifier = candidates[0]['id']
    assert validate_suggestions(json.dumps({'files': [{'id': identifier, 'resolution': quality}]}), candidates)[identifier]['resolution'] == expected


@pytest.mark.parametrize('content,category', [
    ('{"files":[{"id":"ID","episode_number":2,"episode_number":3}]}', 'JSON'),
    ('{"files":[{"id":"ID","episode_number":2.0}]}', '字段类型'),
    ('{"files":[{"id":"ID","episode_number":"2.0"}]}', '字段类型'),
    ('{"files":[{"id":"ID","season_number":3}]}', '冲突'),
    ('prefix {"files":[]}', 'JSON'),
    ('{"files":[{"id":"aaaaaaaaaaaaaaaaaaaaaaaa","episode_number":2}]}', 'ID'),
])
def test_response_categories(content, category):
    candidates = model_candidates(plan(['Show 02.mkv'], TV, season_number=1))
    with pytest.raises(Problem, match=category):
        validate_suggestions(content.replace('ID', candidates[0]['id']), candidates)


@pytest.mark.parametrize('explicit,status,expected_calls', [(True, 400, 2), (True, 422, 2), (False, 400, 1), (True, 500, 1), (True, 401, 1)])
async def test_only_explicit_json_mode_rejection_retries_once(store, monkeypatch, explicit, status, expected_calls):
    vault = MemoryVault()
    configure(store, vault)
    seen = []
    candidates = model_candidates(plan(['Show 02.mkv'], TV, season_number=1))
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(status, json={'error': {'message': 'response_format is not supported' if explicit else 'bad request'}})
        return httpx.Response(200, json=response_for([{'id': candidates[0]['id'], 'episode_number': 2}]))
    install_transport(monkeypatch, handler)
    llm = NamingLLM(store, vault)
    if expected_calls == 2:
        assert await llm.suggest({}, candidates)
        assert 'response_format' in seen[0] and 'response_format' not in seen[1]
    else:
        with pytest.raises(Problem):
            await llm.suggest({}, candidates)
    assert len(seen) == expected_calls


def test_model_candidates_and_precedence_do_not_resolve_conflicts():
    assert not model_candidates(plan(['S01E02.mkv'], TV))
    assert not model_candidates(plan(['movie.1080p.mkv']))
    assert not model_candidates(plan(['movie.mkv'], None))
    for path in ['S02/Show.S01E01.mkv', 'S01E01E02.mkv', 'S1001/Show 01.mkv']:
        assert not model_candidates(plan([path], TV))
    assert not model_candidates(plan(['movie.1080p.2160p.mkv']))
    source = 'S01/Show 02.mkv'
    result = plan([source], TV, suggestions={file_id(source): {'season_number': 9, 'episode_number': 2}})
    assert result['plan'][0]['target'] == 'S01/绝命毒师 S01E02.mkv'
    assert '大模型' in result['plan'][0]['basis']
    result = plan([source], TV, suggestions={file_id(source): {'episode_number': 2}}, overrides={file_id(source): {'episode_number': 3}})
    assert result['plan'][0]['episode_number'] == 3
    result = plan(['movie.1080p.mkv'], suggestions={file_id('movie.1080p.mkv'): {'resolution': '2160P'}})
    assert result['plan'][0]['resolution'] == '1080P'


async def prepare_model_task(store, movie=False):
    worker, task, ol, _ = new_worker(store)
    configure(store, worker.vault)
    task = add_naming(worker, task, MOVIE if movie else TV, None)
    task = await drive(worker, task, 'naming_planning')
    if not movie:
        naming = task['payload']['naming']
        naming['season_number'] = 1
        task = worker.update(task, naming=naming)
    return worker, task, ol


async def test_auto_model_waits_for_confirmation_and_does_not_repeat_on_retry(store):
    worker, task, ol = await prepare_model_task(store)
    calls = enable_rename(ol)
    worker.llm.suggest = AsyncMock(return_value={file_id('open-movie.mkv'): {'episode_number': 2}})
    await worker.step(task)
    job = worker.naming_jobs[task['id']]
    await job
    current = store.task(task['id'])
    assert current['state'] == 'naming_review'
    assert current['payload']['naming']['review_required']
    assert not calls and not ol.move_calls
    assert current['id'] not in [t['id'] for t in store.tasks(active=True)]
    await worker.retry(task['id'])
    assert store.task(task['id'])['state'] == 'naming_review'
    worker.llm.suggest.assert_awaited_once()
    current = worker.update(current, 'renaming')
    with pytest.raises(Problem, match='确认'):
        await worker.step(current)
    assert not calls


async def test_rule_success_skips_llm(store):
    worker, task, ol = await prepare_model_task(store, movie=True)
    naming = task['payload']['naming']
    naming['resolution'] = '1080P'
    task = worker.update(task, naming=naming)
    worker.llm.suggest = AsyncMock(side_effect=AssertionError('unexpected model request'))
    await worker.step(task)
    assert store.task(task['id'])['state'] == 'renaming'
    worker.llm.suggest.assert_not_awaited()


async def test_model_failure_preserves_files_and_manual_retry_does_not_recall(store):
    worker, task, ol = await prepare_model_task(store)
    calls = enable_rename(ol)
    worker.llm.suggest = AsyncMock(side_effect=Problem('大模型请求超时', 504))
    await worker.step(task)
    await worker.naming_jobs[task['id']]
    assert store.task(task['id'])['state'] == 'needs_attention'
    await worker.retry(task['id'])
    await worker.step(store.task(task['id']))
    assert store.task(task['id'])['state'] == 'needs_attention'
    assert worker.llm.suggest.await_count == 1 and not calls and not ol.move_calls
    assert ol.fs[task['payload']['staging']][0]['name'] == 'open-movie.mkv'


async def test_running_job_does_not_hold_worker_lock_and_cancel_discards_result(store):
    worker, task, ol = await prepare_model_task(store)
    entered, release = asyncio.Event(), asyncio.Event()
    async def suggest(*args):
        assert not worker.lock.locked()
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()  # Simulate an upstream call that cannot be canceled.
        return {file_id('open-movie.mkv'): {'episode_number': 2}}
    worker.llm.suggest = suggest
    async with worker.lock:
        await worker.step(task)
    job = worker.naming_jobs[task['id']]
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(worker.cancel(task['id']), 1)
    release.set()
    await job
    assert store.task(task['id'])['state'] == 'canceled'
    assert not store.task(task['id'])['payload']['naming'].get('suggestions')
    assert not ol.move_calls


async def test_restart_marks_running_analysis_interrupted_without_calling_again(store):
    worker, task, _ = await prepare_model_task(store)
    naming = build_plan(task['payload']['naming'])
    naming.update(llm={'status': 'running', 'run_id': 'old-process'}, review_required=True, revision=1)
    task = worker.update(task, 'naming_analyzing', naming=naming)
    restarted = Worker(Store(store.path), worker.vault, worker.openlist, worker.p115)
    restarted.llm.suggest = AsyncMock()
    await restarted.step(task)
    result = store.task(task['id'])
    assert result['state'] == 'needs_attention' and result['payload']['naming']['review_required']
    assert '中断' in result['error']
    restarted.llm.suggest.assert_not_awaited()


async def test_batches_persist_success_before_later_failure(store):
    worker, task, _ = await prepare_model_task(store)
    naming = task['payload']['naming']
    naming['files'] = [{'path': f'clip-{i}.mkv', 'size': 10} for i in range(51)]
    task = worker.update(task, naming=naming)
    async def suggest(naming, candidates):
        if len(candidates) == 1:
            raise Problem('大模型请求超时', 504)
        return {item['id']: {'episode_number': i + 1} for i, item in enumerate(candidates)}
    worker.llm.suggest = AsyncMock(side_effect=suggest)
    task = worker.start_naming_analysis(task)
    await worker.naming_jobs[task['id']]
    result = store.task(task['id'])
    assert result['state'] == 'needs_attention'
    assert len(result['payload']['naming']['suggestions']) == 50
    assert worker.llm.suggest.await_count == 2


def review_task(client, store):
    worker = client.app.state.worker
    task = store.create_task('magnet:' + 'c' * 40, '/115/infuse', {'kind': 'magnet', 'title': 'Preview', 'staging': '/115/staging/test'})
    naming = plan(['Show 02.mkv', 'Show 02.zh.srt'], TV, season_number=1,
                  suggestions={file_id('Show 02.mkv'): {'episode_number': 2, 'reason': '编号02'}},
                  llm={'status': 'completed'}, review_required=True, revision=2, locked=False)
    worker.file_manifest = AsyncMock(return_value=naming['files'])
    worker.manifest_directories = []
    return store.update_task(task['id'], 'naming_review', naming=naming, resume_state='naming_planning')


def test_confirm_revision_idempotence_edit_and_manifest_guards(llm_client):
    client, store, _ = llm_client
    task = review_task(client, store)
    path = '/api/v1/tasks/' + task['id'] + '/naming'
    assert client.post(path + '/confirm', json={'revision': 1}).status_code == 409
    assert client.post(path + '/confirm', json={'revision': True}).status_code == 422
    assert client.post('/api/v1/tasks/' + task['id'] + '/retry', json={}).json()['state'] == 'naming_review'
    result = client.patch(path, json={'files': [{'id': file_id('Show 02.mkv'), 'episode_number': 3}]})
    assert result.status_code == 200 and result.json()['revision'] == 3
    assert result.json()['review_required'] and store.task(task['id'])['state'] == 'naming_review'
    assert result.json()['files'][0]['target'] == '绝命毒师 S01E03.mkv'
    assert result.json()['files'][1]['target'] == '绝命毒师 S01E03.zh.srt'
    assert client.post(path + '/confirm', json={'revision': 2}).status_code == 409
    worker = client.app.state.worker
    worker.file_manifest.return_value = []
    assert client.post(path + '/confirm', json={'revision': 3}).status_code == 409
    worker.file_manifest.return_value = task['payload']['naming']['files']
    result = client.post(path + '/confirm', json={'revision': 3})
    assert result.status_code == 200 and not result.json()['review_required']
    assert store.task(task['id'])['state'] == 'renaming'
    assert client.post(path + '/confirm', json={'revision': 3}).status_code == 200
    # An edit before the first remote write invalidates approval, including on restart.
    edited = client.patch(path, json={'files': [{'id': file_id('Show 02.mkv'), 'episode_number': 4}]}).json()
    assert edited['review_required'] and edited['revision'] == 4
    assert store.task(task['id'])['state'] == 'naming_review'
    assert client.post(path + '/confirm', json={'revision': 3}).status_code == 409
    store.update_task(task['id'], 'renaming', naming={**store.task(task['id'])['payload']['naming'], 'locked': True})
    assert client.patch(path, json={}).status_code == 409
    assert client.post(path + '/analyze', json={}).status_code == 409


def test_check_uses_saved_configuration_and_is_authenticated(llm_client):
    client, _, _ = llm_client
    client.app.state.llm.check = AsyncMock(return_value={'ok': True, 'message': 'checked'})
    assert client.post('/api/v1/llm/check', json={}).json()['ok']
    client.post('/api/v1/auth/logout')
    assert client.post('/api/v1/llm/check', json={}).status_code == 401
    client.app.state.llm.check.assert_awaited_once()


@pytest.mark.parametrize('episode,ok', [(2, True), (3, False)])
async def test_real_check_validates_fixed_sample(store, monkeypatch, episode, ok):
    vault = MemoryVault()
    configure(store, vault)
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=response_for([{'id': '0' * 24, 'episode_number': episode}])))
    llm = NamingLLM(store, vault)
    if ok:
        assert (await llm.check())['ok']
    else:
        with pytest.raises(Problem, match='未通过'):
            await llm.check()


async def test_completed_analysis_can_be_regenerated_and_invalidates_old_approval(store):
    worker, task, _ = await prepare_model_task(store)
    worker.llm.suggest = AsyncMock(return_value={file_id('open-movie.mkv'): {'episode_number': 2}})
    worker.start_naming_analysis(task)
    await worker.naming_jobs[task['id']]
    old_revision = store.task(task['id'])['payload']['naming']['revision']
    worker.llm.suggest.return_value = {file_id('open-movie.mkv'): {'episode_number': 3}}
    worker.start_naming_analysis(store.task(task['id']))
    await worker.naming_jobs[task['id']]
    current = store.task(task['id'])
    assert current['state'] == 'naming_review'
    assert current['payload']['naming']['revision'] > old_revision
    assert current['payload']['naming']['plan'][0]['episode_number'] == 3


async def test_manual_correction_discards_late_job_result(store, llm_client):
    client, store, _ = llm_client
    worker = client.app.state.worker
    task = review_task(client, store)
    source = 'Show 02.mkv'
    naming = task['payload']['naming']
    naming['suggestions'] = {}
    naming['llm'] = {'status': 'failed'}
    task = worker.update(task, 'needs_attention', naming=build_plan(naming), resume_state='naming_planning')
    store.configure({'llm_enabled': True})
    entered, release = asyncio.Event(), asyncio.Event()
    async def suggest(*args):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {file_id(source): {'episode_number': 2}}
    worker.llm.suggest = suggest
    worker.start_naming_analysis(task)
    job = worker.naming_jobs[task['id']]
    await entered.wait()
    response = client.patch('/api/v1/tasks/' + task['id'] + '/naming', json={'files': [{'id': file_id(source), 'episode_number': 4}]})
    assert response.status_code == 200
    revision = response.json()['revision']
    release.set()
    await job
    current = store.task(task['id'])
    assert current['payload']['naming']['revision'] == revision
    assert current['payload']['naming']['plan'][0]['episode_number'] == 4
    assert not current['payload']['naming']['suggestions']
    assert current['state'] == 'naming_review'


async def test_confirmed_model_ingest_renames_subtitles_and_reaches_scan(store):
    worker, task, ol = await prepare_model_task(store, movie=True)
    staging = task['payload']['staging']
    ol.fs[staging].append({'name': 'open-movie.zh.srt', 'size': 30})
    naming = task['payload']['naming']
    naming['files'] = await worker.file_manifest(staging)
    task = worker.update(task, naming=naming)
    calls = enable_rename(ol)
    worker.llm.suggest = AsyncMock(return_value={file_id('open-movie.mkv'): {'resolution': '1080P'}})
    await worker.step(task)
    await worker.naming_jobs[task['id']]
    task = store.task(task['id'])
    assert task['state'] == 'naming_review' and not calls and not ol.move_calls
    naming = task['payload']['naming']
    naming.update(review_required=False, confirmed_revision=naming['revision'])
    task = worker.update(task, 'renaming', naming=naming)
    result = await drive(worker, task, 'scanning', iterations=40)
    assert result['state'] == 'scanning'
    assert [name for _, name in calls] == ['Interstellar 1080P.mkv', 'Interstellar 1080P.zh.srt']
    assert result['payload']['videos'] == ['Interstellar 1080P.mkv']
