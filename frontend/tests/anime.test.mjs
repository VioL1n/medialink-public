import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source=await readFile(new URL('../src/anime.ts',import.meta.url),'utf8')
const {outputText}=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}})
const {orderedDays,defaultDay,splitWords,ruleSummary,episodeStatus,checkLabel}=await import('data:text/javascript;base64,'+Buffer.from(outputText).toString('base64'))
const day=(weekday,count=1)=>({weekday,label:String(weekday),items:Array.from({length:count},(_,id)=>({id,title:'番'}))})

test('days run Monday to Sunday, then films and OVAs',()=>{
  assert.deepEqual(orderedDays([day(8),day(0),day(6),day(7),day(1)]).map(d=>d.weekday),[1,6,0,7,8])
})

test('the default day is today unless today is empty',()=>{
  const friday=new Date(2026,9,16)
  assert.equal(defaultDay([day(1),day(5)],friday),5)
  assert.equal(defaultDay([day(0),day(5,0),day(3)],friday),3)
  assert.equal(defaultDay([],friday),5)
})

test('keywords split on spaces and commas without duplicates',()=>{
  assert.deepEqual(splitWords(' 简体, CHS，简体  HEVC '),['简体','CHS','HEVC'])
  assert.deepEqual(splitWords(''),[])
})

test('rule summaries name resolution and keywords',()=>{
  assert.equal(ruleSummary({resolution:'1080P',include:['简体'],exclude:['HEVC','繁体']}),'1080P · 包含 简体 · 排除 HEVC、繁体')
  assert.equal(ruleSummary({resolution:null,include:[],exclude:[]}),'不限清晰度')
})

test('episode status follows the ingest task',()=>{
  const episode=(extra)=>({episode:1,title:'',created:0,...extra})
  assert.deepEqual(episodeStatus(episode({})),{label:'已跳过',tone:'skipped'})
  assert.equal(episodeStatus(episode({task_id:'a',state:'completed'})).tone,'done')
  assert.equal(episodeStatus(episode({task_id:'a',state:'naming_review'})).tone,'attention')
  assert.equal(episodeStatus(episode({task_id:'a',state:'downloading'})).label,'入库中')
})

test('check times read relative to now',()=>{
  const now=Date.UTC(2026,9,17,12)
  assert.equal(checkLabel(0,now),'尚未检查')
  assert.equal(checkLabel(now/1000+600,now),'10 分钟后')
  assert.equal(checkLabel(now/1000-7200,now),'2 小时前')
})
