import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source=await readFile(new URL('../src/settingsState.ts',import.meta.url),'utf8')
const {outputText}=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}})
const {copySettings,mergeSettingsResponse,reconcileDraft,settingsPatch,reconcileSourceSelection}=await import('data:text/javascript;base64,'+Buffer.from(outputText).toString('base64'))

test('saving search settings preserves an unsubmitted proxy and API draft',()=>{
  const previous={enabled_search_sources:['nyaa'],proxy:'',telegram_api_id:'',default_use_tmdb:true}
  const draft={...previous,enabled_search_sources:[],proxy:'socks5://127.0.0.1:7890',telegram_api_id:'123'}
  const next={...previous,enabled_search_sources:[]}
  const result=reconcileDraft(previous,draft,next,['enabled_search_sources'])
  assert.deepEqual(result,draft)
  assert.equal(settingsPatch(result,['proxy']).proxy,'socks5://127.0.0.1:7890')
  assert.deepEqual(settingsPatch(result,['enabled_search_sources','default_use_tmdb']),{enabled_search_sources:[],default_use_tmdb:true})
})
test('refreshing service state preserves edits and updates clean fields',()=>{
  const previous={proxy:'',tmdb_configured:false,public_access_hosts:''}
  const draft={...previous,proxy:'draft'}
  const next={proxy:'',tmdb_configured:true,public_access_hosts:'example.org'}
  assert.deepEqual(reconcileDraft(previous,draft,next),{proxy:'draft',tmdb_configured:true,public_access_hosts:'example.org'})
})
test('submitted values adopt backend normalization and copied arrays stay independent',()=>{
  const previous={enabled_search_sources:['nyaa'],public_access_hosts:''}
  const draft={enabled_search_sources:['nyaa','nyaa'],public_access_hosts:' EXAMPLE.ORG '}
  const next={enabled_search_sources:['nyaa'],public_access_hosts:'example.org'}
  const result=reconcileDraft(previous,draft,next,Object.keys(draft))
  assert.deepEqual(result,next)
  result.enabled_search_sources.push('telegram')
  assert.deepEqual(next.enabled_search_sources,['nyaa'])
  assert.deepEqual(copySettings(previous),previous)
})
test('source changes remove disabled sources and select newly enabled sources',()=>{
  assert.deepEqual(reconcileSourceSelection(['nyaa','telegram'],['nyaa'],['telegram','knaben']),['knaben'])
  assert.deepEqual(reconcileSourceSelection(['nyaa'],['nyaa'],[]),[])
  assert.deepEqual(reconcileSourceSelection([],[],['nyaa','telegram']),['nyaa','telegram'])
})
test('refreshing identical source IDs does not restore a temporary empty selection',()=>{
  assert.deepEqual(reconcileSourceSelection(['nyaa','telegram'],[],['nyaa','telegram']),[])
})
test('a delayed search save response preserves a completed network save',()=>{
  const latest={proxy:'socks5://127.0.0.1:7890',enabled_search_sources:['nyaa'],default_use_tmdb:true}
  const delayed={proxy:'',enabled_search_sources:[],default_use_tmdb:false}
  assert.deepEqual(mergeSettingsResponse(latest,delayed,['enabled_search_sources','default_use_tmdb']),{...latest,enabled_search_sources:[],default_use_tmdb:false})
})
test('credential saves update configuration flags without accepting unrelated snapshots',()=>{
  const latest={proxy:'draft',tmdb_configured:false,tmdb_auth_type:null,telegram_hash_saved:false}
  const next={proxy:'old',tmdb_configured:true,tmdb_auth_type:'token',telegram_hash_saved:true}
  assert.deepEqual(mergeSettingsResponse(latest,next,['tmdb_credential']),{...latest,tmdb_configured:true,tmdb_auth_type:'token'})
  assert.equal(mergeSettingsResponse(latest,next,['telegram_api_hash']).telegram_hash_saved,true)
})

test('LLM saves reconcile credential flags while preserving other completed saves',()=>{
  const latest={proxy:'saved-proxy',llm_configured:false,llm_key_saved:false,llm_enabled:false,llm_model:'old'}
  const next={proxy:'stale-proxy',llm_configured:true,llm_key_saved:true,llm_enabled:true,llm_model:'new'}
  assert.deepEqual(mergeSettingsResponse(latest,next,['llm_api_key','llm_enabled','llm_model']),{...latest,llm_configured:true,llm_key_saved:true,llm_enabled:true,llm_model:'new'})
  assert.deepEqual(mergeSettingsResponse({...latest,llm_enabled:true},latest,['llm_api_key']),latest)
  const draft={...latest,proxy:'unsubmitted-proxy',llm_model:'new'}
  assert.equal(reconcileDraft(latest,draft,{...latest,llm_model:'normalized'},['llm_model']).proxy,'unsubmitted-proxy')
})
