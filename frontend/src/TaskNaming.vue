<script setup lang="ts">
import {computed,ref,watch} from 'vue'
import MediaPicker from './MediaPicker.vue'
import type {MediaSelection,Naming,RequestFn} from './types'
const props=defineProps<{request:RequestFn;taskId:string;naming:Naming;llmEnabled?:boolean;preview?:boolean}>()
const emit=defineEmits<{updated:[];dirty:[boolean]}>()
const media=ref<MediaSelection|null>(null), mediaTitle=ref(''), quality=ref(''), saving=ref(false), error=ref('')
const corrections=ref<Record<string,{resolution:string;season_number:string;episode_number:string}>>({})
const qualities=['480P','576P','720P','1080P','1080I','2160P','4320P']
const videos=computed(()=>props.naming.files.filter(f=>f.kind==='video'))
function values(n:Naming){return {media:n.media?{type:n.media.type,id:n.media.id,season_number:n.season_number??undefined}:null,quality:n.resolution||'',corrections:Object.fromEntries(n.files.filter(f=>f.kind==='video').map(f=>[f.id,{resolution:f.resolution||'',season_number:f.season_number==null?'':String(f.season_number),episode_number:f.episode_number==null?'':String(f.episode_number)}]))}}
const baseline=ref('')
const dirty=computed(()=>JSON.stringify({media:media.value,quality:quality.value,corrections:corrections.value})!==baseline.value)
watch(dirty,value=>emit('dirty',value),{immediate:true})
const endpoint=computed(()=>props.preview?'/naming/previews/'+props.taskId:'/tasks/'+props.taskId+'/naming')
const analyzing=computed(()=>props.naming.llm?.status==='running')
const canAnalyze=computed(()=>props.llmEnabled&&videos.value.some(f=>f.missing_fields?.length||(props.naming.llm?.status==='completed'&&f.basis?.includes('大模型'))))
function reset(n=props.naming){const v=values(n);media.value=v.media;mediaTitle.value=n.media?.title||'';quality.value=v.quality;corrections.value=v.corrections;baseline.value=JSON.stringify(v)}
watch(()=>props.taskId,()=>reset(),{immediate:true})
watch(()=>props.naming,(n,previous)=>{
  if(!dirty.value||!n.editable){reset(n);return}
  const before=values(previous), after=values(n), nextBaseline=JSON.stringify(values(n))
  if(JSON.stringify(media.value)===JSON.stringify(before.media)){media.value=after.media;mediaTitle.value=n.media?.title||''}
  if(quality.value===before.quality)quality.value=after.quality
  const next=after.corrections
  for(const [id,fields] of Object.entries(next)){
    const old=before.corrections[id], draft=corrections.value[id]
    if(old&&draft)for(const key of ['resolution','season_number','episode_number'] as const)if(draft[key]!==old[key])fields[key]=draft[key]
  }
  corrections.value=next;baseline.value=nextBaseline
})
async function save(){saving.value=true;error.value='';try{const current=props.naming;const changedMedia=media.value?.id!==current.media?.id||media.value?.type!==current.media?.type||(media.value?.season_number??null)!==(current.season_number??null);const files=videos.value.flatMap(f=>{const c=corrections.value[f.id];const values:Record<string,unknown>={id:f.id};if(c.resolution!==(f.resolution||''))values.resolution=c.resolution||null;if(c.season_number!==(f.season_number==null?'':String(f.season_number)))values.season_number=c.season_number===''?null:Number(c.season_number);if(c.episode_number!==(f.episode_number==null?'':String(f.episode_number)))values.episode_number=c.episode_number===''?null:Number(c.episode_number);return Object.keys(values).length>1?[values]:[]});const saved=await props.request(endpoint.value,'PATCH',props.preview?{revision:props.naming.revision,files}:{...(changedMedia?{media:media.value}:{}),resolution:quality.value||null,files});reset(saved);emit('updated')}catch(e:any){error.value=e.message}finally{saving.value=false}}
// Library plans let the user override curation: bring an excluded file back, or leave one out.
async function choose(id:string,included:boolean){saving.value=true;error.value='';try{const saved=await props.request<Naming>(endpoint.value,'PATCH',props.preview?{revision:props.naming.revision,files:[{id,included}]}:{files:[{id,included}]});reset(saved);emit('updated')}catch(e:any){error.value=e.message}finally{saving.value=false}}
const library=computed(()=>props.naming.version===2)
async function namingAction(action:'confirm'|'analyze'){
  saving.value=true;error.value=''
  try{const saved=await props.request<Naming>(endpoint.value+'/'+action,'POST',action==='confirm'?{revision:props.naming.revision||0}:{});reset(saved);emit('updated')}
  catch(e:any){error.value=e.message;emit('updated')}
  finally{saving.value=false}
}
</script>
<template>
  <section class="task-naming">
    <h3>规范命名</h3>
    <p v-if="library" class="modal-note">整理到「{{naming.folder||'中文名 (年份) [tmdbid=…]'}}」：电影为 原语片名 (年份) - 清晰度；剧集放入 Season NN，命名为 中文名 S01E01。广告、样片和非影视文件不入库。</p>
    <p v-else class="modal-note">电影：TMDB 原语片名 + 清晰度；剧集：TMDB 中文名（无中文时用原名）+ S01E01。保留目录层级。</p>
    <p v-if="naming.llm?.message" class="modal-note" role="status">{{naming.llm.message}}</p>
    <p v-if="naming.review_required&&!preview" class="naming-review-note">请核对完整命名预览。保存修改后仍需点击“确认并重命名”。</p>
    <template v-if="naming.editable&&!preview">
      <MediaPicker :request="request" v-model="media" v-model:title="mediaTitle" :disabled="saving"/>
      <label v-if="media?.type==='movie'">资源清晰度（文件名与目录优先）<select v-model="quality" aria-label="资源清晰度" :disabled="saving"><option value="">未指定</option><option v-for="q in qualities" :key="q">{{q}}</option></select></label>
    </template>
    <p v-else-if="!naming.editable" class="modal-note">{{naming.locked?'命名计划已锁定，重试将继续核对并完成原计划。':'该任务已停止，命名信息仅供查看。'}}</p>
    <p v-if="!naming.files.length" class="modal-note">文件到位后显示命名预览。</p>
    <div v-for="file in naming.files" :key="file.id" class="naming-file">
      <p>{{file.source}}</p><p class="naming-target">→ {{file.destination||file.target||'等待补全'}} <small>{{file.status==='done'?'已核对':file.status==='attempted'?'正在核对':''}}</small></p>
      <small>依据：{{file.basis||'等待关联 TMDB'}}</small><button v-if="library&&naming.editable" type="button" class="text-button naming-choice" :disabled="saving||dirty" @click="choose(file.id,false)">排除此文件</button><small v-if="file.llm_reason" class="naming-reason">模型依据：{{file.llm_reason}}</small><p v-if="file.error" class="error-text">{{file.error}}</p>
      <div v-if="naming.editable&&file.kind==='video'&&corrections[file.id]" class="naming-fields">
        <label v-if="media?.type==='movie'">清晰度<select v-model="corrections[file.id]!.resolution" :aria-label="file.source+' 清晰度'" :disabled="saving"><option value="">自动识别</option><option v-for="q in qualities" :key="q">{{q}}</option></select></label>
        <template v-if="media?.type==='tv'"><label>季<input v-model="corrections[file.id]!.season_number" type="number" min="0" max="1000" :aria-label="file.source+' 季号'" :disabled="saving"/></label><label>集<input v-model="corrections[file.id]!.episode_number" type="number" min="1" max="9999" :aria-label="file.source+' 集号'" :disabled="saving"/></label></template>
      </div>
    </div>
    <details v-if="naming.excluded?.length" class="naming-excluded"><summary>已排除 {{naming.excluded.length}} 个文件，保留在暂存目录</summary>
      <div v-for="file in naming.excluded" :key="file.id" class="naming-file"><p>{{file.source}}</p><small>{{file.label}}：{{file.reason}}</small><button v-if="naming.editable&&file.kind!=='other'" type="button" class="text-button naming-choice" :disabled="saving||dirty" @click="choose(file.id,true)">改为入库</button></div>
    </details>
    <p v-for="message in naming.errors" :key="message" class="error-text">{{message}}</p>
    <p v-for="message in naming.warnings" :key="message" class="modal-note">{{message}}</p>
    <p v-if="error" class="error-text" role="alert">{{error}}</p>
    <p v-if="dirty&&naming.review_required" class="modal-note">有未保存的修改，请先保存预览。</p>
    <div v-if="naming.editable" class="naming-actions">
      <button type="button" class="primary wide" :disabled="saving||(!!naming.review_required&&!dirty)" @click="save">{{saving?'处理中':naming.review_required?'保存命名预览':'保存命名信息并继续'}}</button>
      <button v-if="llmEnabled||naming.llm?.status" type="button" class="outline wide" :disabled="saving||dirty||analyzing||!canAnalyze" @click="namingAction('analyze')">{{analyzing?'大模型识别中':naming.llm?.status?'重新识别':'使用大模型补全'}}</button>
      <button v-if="naming.review_required&&!preview" type="button" class="primary wide" :disabled="saving||dirty||analyzing||!!naming.errors.length||!naming.files.length" @click="namingAction('confirm')">确认并重命名</button>
    </div>
  </section>
</template>
