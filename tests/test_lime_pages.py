import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from medialink.app import create_app
from medialink.core import DEFAULTS, Problem, Store
from medialink.web_sources import Knaben, LimeTorrents
from test_medialink import MAGNET, MemoryVault, authenticate
from test_web_sources import knaben_hit, lime_row

# Matches the observed empty page; sponsored links remain present without .table2.
EMPTY_PAGE = '''<title>No match Torrents Download - LimeTorrents.fun</title>
<form id="searchform" action="/post/search.php"><input name="q" value="no match"></form>
<div id="content"><h2>Search Results for: <span class="querytext">no match</span></h2>
<table class="table3"><tr><td><a href="/leet2/?search=1">Sponsored download</a></td></tr></table>
<h2>No results found</h2></div>
<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'''
RESULT_PAGE = '<table class="table2">'+lime_row(1)+lime_row(2)+'</table>'
DETAIL_PAGE = '<div class="torrentinfo"><a href="'+MAGNET+'">Magnet</a></div>'


def source_with(handler):
    source = LimeTorrents(lambda: DEFAULTS)
    source.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return source


async def test_lime_empty_results_are_successful_and_cached():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=EMPTY_PAGE)
    source = source_with(handler)
    assert await source.search('电影') == []
    assert await source.search('电影') == []
    assert len(calls) == 1


@pytest.mark.parametrize('body', [
    '<h2>No results found</h2>',
    EMPTY_PAGE.replace('id="searchform"', 'id="unknown"'),
    EMPTY_PAGE.replace('class="querytext"', 'class="unknown"'),
    EMPTY_PAGE.replace('No results found', 'Unrecognized page'),
])
def test_lime_unknown_pages_are_not_empty_success(body):
    with pytest.raises(Problem, match='页面结构无法识别'):
        LimeTorrents.parse_search(body, 'https://example.org')


def test_lime_normal_cf_scripts_do_not_trigger_validation():
    script = '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
    assert len(LimeTorrents.parse_search(RESULT_PAGE+script, 'https://example.org')) == 2
    item = LimeTorrents.parse_search(RESULT_PAGE, 'https://example.org')[0]
    assert LimeTorrents.parse_detail(DETAIL_PAGE+script, item)[0]['link'] == MAGNET


@pytest.mark.parametrize('status,body,headers', [
    (200, EMPTY_PAGE, {'cf-mitigated':'challenge'}),
    (403, 'private-response-secret', {'cf-mitigated':'challenge'}),
    (200, '<title>Just a moment...</title>', {}),
    (403, '<form id="challenge-form"></form>', {}),
    (200, '<h1>Verify you are human</h1>', {}),
    (200, '<form action="/cdn-cgi/challenge-platform/verify"></form>', {}),
])
async def test_lime_search_detects_real_challenges_before_status(status,body,headers):
    source = source_with(lambda request: httpx.Response(status, text=body, headers=headers))
    with pytest.raises(Problem, match='站点要求人机验证') as error:
        await source.search('film')
    assert 'private-response-secret' not in str(error.value)
    assert not source.cache


@pytest.mark.parametrize('failure', [httpx.Response(403, text='Forbidden'), httpx.ReadTimeout('private-secret')])
async def test_lime_network_failure_is_not_validation(failure):
    def handler(request):
        if isinstance(failure, Exception):
            raise failure
        return failure
    with pytest.raises(Problem, match='网络访问失败') as error:
        await source_with(handler).search('film')
    assert 'private-secret' not in str(error.value)
    assert '人机验证' not in str(error.value)


@pytest.mark.parametrize('status,body,headers,message', [
    (200, '<h1>Verify you are human</h1>', {}, '站点要求人机验证'),
    (403, 'private-secret', {'cf-mitigated':'challenge'}, '站点要求人机验证'),
    (503, 'Unavailable', {}, '网络访问失败'),
    (200, '<h1>Site layout changed</h1>', {}, '页面结构无法识别'),
    (200, '<div class="torrentinfo">Torrent info without magnet</div>', {}, '详情没有有效磁力'),
    (200, '<div class="torrentinfo"><a href="magnet:?xt=invalid">Invalid</a></div>', {}, '详情没有有效磁力'),
])
async def test_lime_all_details_failed_show_the_cause(status,body,headers,message):
    def handler(request):
        if request.url.path.startswith('/search/'):
            return httpx.Response(200, text=RESULT_PAGE)
        return httpx.Response(status, text=body, headers=headers)
    source = source_with(handler)
    with pytest.raises(Problem, match=message):
        await source.search('film')
    assert not source.cache


async def test_lime_partial_challenge_keeps_successful_details():
    def handler(request):
        if request.url.path.startswith('/search/'):
            return httpx.Response(200, text=RESULT_PAGE)
        if request.url.path.endswith('-1.html'):
            return httpx.Response(403, headers={'cf-mitigated':'challenge'})
        return httpx.Response(200, text=DETAIL_PAGE)
    results = await source_with(handler).search('film')
    assert len(results) == 1 and results[0]['sources'][0]['url'].endswith('-2.html')


async def test_lime_mixed_detail_failures_prioritize_challenge():
    def handler(request):
        if request.url.path.startswith('/search/'):
            return httpx.Response(200, text=RESULT_PAGE)
        if request.url.path.endswith('-1.html'):
            return httpx.Response(503)
        return httpx.Response(200, text='<title>Just a moment...</title>')
    with pytest.raises(Problem, match='站点要求人机验证'):
        await source_with(handler).search('film')


@pytest.mark.parametrize('challenged', [False, True])
def test_lime_sse_empty_or_challenge_does_not_block_other_sources(tmp_path, challenged):
    app = create_app(Store(tmp_path/'test.db'), MemoryVault(), start_workers=False)
    resources = Knaben.parse({'hits':[knaben_hit()]})
    app.state.knaben.search = AsyncMock(return_value=resources)
    app.state.limetorrents.client = source_with(lambda request: httpx.Response(
        403 if challenged else 200, text=EMPTY_PAGE,
        headers={'cf-mitigated':'challenge'} if challenged else {})).client
    with TestClient(app) as client:
        authenticate(client)
        job = client.post('/api/v1/search', json={'query':'电影','sources':['knaben','limetorrents']}).json()
        events = client.get('/api/v1/search/'+job['id']+'/events').text
        messages = [json.loads(part.split('\n')[0]) for part in events.split('event: result\ndata: ')[1:]]
        lime = next(message for message in reversed(messages) if message['provider']=='limetorrents')
        assert lime['results'] == []
        assert lime['status'] == ('error' if challenged else 'done')
        assert ('error' in lime) == challenged
        done = json.loads(events.split('event: done\ndata: ')[1].split('\n')[0])['results']
        assert [{k:v for k,v in r.items() if k != 'matched_queries'} for r in done] == resources
