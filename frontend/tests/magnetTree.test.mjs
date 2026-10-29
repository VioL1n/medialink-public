import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'
const source=await readFile(new URL('../src/magnetTree.ts',import.meta.url),'utf8')
const {outputText}=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}})
const {magnetTree,toggleFiles,defaultMagnetFiles,fileBytes}=await import('data:text/javascript;base64,'+Buffer.from(outputText).toString('base64'))
const files=[{id:'1',path:'合集/季一/episode.mkv',size:100,kind:'video'},{id:'2',path:'合集/季一/episode.srt',size:20,kind:'subtitle'},{id:'3',path:'合集/季二/episode.mkv',size:200,kind:'video'},{id:'4',path:'合集/readme.txt',size:10,kind:'other'}]
test('directory tree preserves repeated names and aggregates selection and sizes',()=>{
  const roots=magnetTree(files)
  assert.equal(roots.length,1)
  assert.equal(roots[0].size,330)
  assert.deepEqual(roots[0].ids,['1','2','3','4'])
  assert.deepEqual(roots[0].children[0].ids,['1','2'])
  assert.equal(roots[0].children[1].children[0].file.id,'3')
})
test('defaults include videos and subtitles; directory toggles preserve other selections',()=>{
  assert.deepEqual(defaultMagnetFiles(files),['1','2','3'])
  assert.deepEqual(toggleFiles(['1','2','3'],['1','2'],false),['3'])
  assert.deepEqual(toggleFiles(['3'],['1','2'],true),['3','1','2'])
  assert.deepEqual(toggleFiles([],['1','2'],true),['1','2'])
})
test('curated previews default to the server recommendation, older previews to videos and subtitles',()=>{
  const curated=[{...files[0],keep:true},{...files[1],keep:true},{...files[2],role:'ad',keep:false},{...files[3],role:'other',keep:false}]
  assert.deepEqual(defaultMagnetFiles(curated),['1','2'])
  assert.deepEqual(defaultMagnetFiles([...curated.slice(0,2),files[2]]),['1','2','3'])
})
test('file sizes include empty files and small subtitles',()=>{
  assert.equal(fileBytes(0),'0 B')
  assert.equal(fileBytes(30),'30 B')
  assert.equal(fileBytes(1024**3),'1.00 GB')
})
