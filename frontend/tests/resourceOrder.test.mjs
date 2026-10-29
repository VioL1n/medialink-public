import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source=await readFile(new URL('../src/resourceOrder.ts',import.meta.url),'utf8')
const {outputText}=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}})
const {sortResources,mergeResourceResults}=await import('data:text/javascript;base64,'+Buffer.from(outputText).toString('base64'))
const resource=(key,resolution,score=0)=>({key,resolution,match:{score}})

test('all resolution tiers, equivalent 2160P/4K and unknowns',()=>{
  const values=[undefined,'unknown','480P','720P','1080I','1080P','2160P','4K','8K']
  const input=values.map((q,i)=>resource(String(i),q))
  assert.deepEqual(sortResources(input).map(r=>r.resolution),['8K','2160P','4K','1080P','1080I','720P','480P',undefined,'unknown'])
  assert.deepEqual(input.map(r=>r.resolution),values)
})
test('quality is primary, match score only breaks ties, stable equal scores',()=>{
  const input=[resource('a',' 2160p ',60),resource('b','4k',90),resource('c','2160P',60),resource('d','4K',-40),resource('e',undefined,90),resource('f','1080P')]
  assert.deepEqual(sortResources(input).map(r=>r.key),['b','a','c','d','f','e'])
})
test('missing metadata and quality do not throw',()=>{
  const input=[{key:'a'},{key:'b',resolution:'4K'},{key:'c',resolution:null,match:null}]
  assert.deepEqual(sortResources(input).map(r=>r.key),['b','a','c'])
})

const hit=(key,resolution,score=0)=>({...resource(key,resolution,score),sources:[],matched_queries:[]})
test('a late high-resolution batch moves above matched lower resolution',()=>{
  const low=hit('low','1080P',90),high=hit('high','2160P',-40)
  const initial=mergeResourceResults([],[low])
  assert.deepEqual(mergeResourceResults(initial,[high,low]).map(r=>r.key),['high','low'])
})
test('done preserves existing tie order while removing absent results',()=>{
  const a=hit('a','2160P',60),b=hit('b','4K',60),old=hit('old','8K')
  assert.deepEqual(mergeResourceResults([old,b,a],[a,b],true).map(r=>r.key),['b','a'])
})
test('merge retains sources and matched queries and sorts updated scores',()=>{
  const nyaa={provider:'nyaa',url:'https://example.org/nyaa'},knaben={provider:'knaben',url:'https://example.org/knaben'}
  const a={...hit('a','4K',0),sources:[nyaa],matched_queries:['中文名']},b=hit('b','2160P',60)
  const updated={...a,match:{score:90},sources:[nyaa,knaben],matched_queries:['英文名']}
  const result=mergeResourceResults([b,a],[updated])
  assert.deepEqual(result.map(r=>r.key),['a','b'])
  assert.deepEqual(result[0].sources,[nyaa,knaben])
  assert.deepEqual(result[0].matched_queries,['中文名','英文名'])
  assert.deepEqual(a.sources,[nyaa])
})
