import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source=await readFile(new URL('../src/homeFeed.ts',import.meta.url),'utf8')
const {outputText}=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}})
const {resourceChips,ratingList,openTarget,updatedLabel,kindLabel}=await import('data:text/javascript;base64,'+Buffer.from(outputText).toString('base64'))
const item=(extra={})=>({id:'mv/a',detail:'',kind:'movie',title:'魔法情缘',tags:[],...extra})

test('resource chips skip empty counts and join quality labels',()=>{
  const chips=resourceChips(item({counts:{magnet:36,cloud:193,online:0},qualities:['4K','BD']}))
  assert.deepEqual(chips.map(chip=>[chip.kind,chip.label]),[['magnet','36'],['cloud','193'],['quality','4K BD']])
  assert.deepEqual(resourceChips(item()),[])
})

test('ratings keep source order and show heat as given',()=>{
  const list=ratingList(item({ratings:{douban:5.1,imdb:6,mal:7.45},heat:'1.9万'}))
  assert.deepEqual(list.map(rating=>rating.label),['5.1','6.0','7.5','1.9万'])
  assert.deepEqual(ratingList(item()),[])
  const local=ratingList(item({ratings:{tmdb:8.25,imdb:7.5},heat:'2876'}))
  assert.deepEqual(local.map(rating=>[rating.kind,rating.label]),[['imdb','7.5'],['tmdb','8.3'],['heat','2876']])
})

test('linked titles open details and others search by title',()=>{
  assert.deepEqual(openTarget(item({media:{type:'movie',id:42}})),{media:{type:'movie',id:42}})
  assert.deepEqual(openTarget(item({media:null})),{search:'魔法情缘'})
  assert.equal(kindLabel(item({kind:'anime'})),'动漫')
})

test('update time is relative for the last day',()=>{
  const now=Date.UTC(2026,9,16,12)
  assert.equal(updatedLabel(null,now),'')
  assert.equal(updatedLabel(now/1000-20,now),'刚刚更新')
  assert.equal(updatedLabel(now/1000-25*60,now),'25 分钟前更新')
  assert.equal(updatedLabel(now/1000-3*3600,now),'3 小时前更新')
})
