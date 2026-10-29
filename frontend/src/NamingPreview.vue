<script setup lang="ts">
import {computed,onUnmounted,ref,watch} from 'vue'
import TaskNaming from './TaskNaming.vue'
import type {NamingPreview,NamingPreviewReference,RequestFn} from './types'
const props=defineProps<{request:RequestFn;input:Record<string,unknown>;available:boolean;llmEnabled:boolean}>()
const emit=defineEmits<{approved:[NamingPreviewReference|null];blocked:[boolean]}>()
const draft=ref<NamingPreview|null>(null), engaged=ref(false), working=ref(false), dirty=ref(false), error=ref('')
let generation=0, timer:ReturnType<typeof setTimeout>|undefined, alive=true
const ready=computed(()=>engaged.value&&!working.value&&!dirty.value&&draft.value?.status==='ready'&&!draft.value.naming.errors.length)
watch(ready,()=>{emit('approved',ready.value&&draft.value?{preview_id:draft.value.preview_id,revision:draft.value.revision}:null);emit('blocked',engaged.value&&!ready.value)},{immediate:true})
watch(engaged,()=>emit('blocked',engaged.value&&!ready.value))
function erase(id:string){void props.request('/naming/previews/'+id,'DELETE').catch(()=>{})}
function cancel(){generation++;clearTimeout(timer);if(draft.value)erase(draft.value.preview_id);draft.value=null;engaged.value=false;working.value=false;dirty.value=false;error.value='';emit('approved',null);emit('blocked',false)}
watch(()=>JSON.stringify(props.input),cancel)
onUnmounted(()=>{alive=false;cancel()})
function accept(next:NamingPreview,token:number){if(!alive||token!==generation){return false}if(draft.value&&next.preview_id===draft.value.preview_id&&next.revision<draft.value.revision)return false;draft.value=next;return true}
function poll(token:number){clearTimeout(timer);if(!draft.value||!['preparing','analyzing'].includes(draft.value.status))return;timer=setTimeout(async()=>{try{const id=draft.value?.preview_id;if(!id||token!==generation)return;const next=await props.request<NamingPreview>('/naming/previews/'+id);if(accept(next,token))poll(token)}catch(e:any){if(token===generation&&alive)error.value=e.message}},800)}
async function create(){cancel();const token=generation;engaged.value=true;working.value=true;error.value='';try{const next=await props.request<NamingPreview>('/naming/previews','POST',props.input);if(!accept(next,token)){erase(next.preview_id);return}poll(token)}catch(e:any){if(token===generation&&alive)error.value=e.message}finally{if(token===generation&&alive)working.value=false}}
const previewRequest:RequestFn=async <T=any>(path:string,method?:string,body?:unknown)=>{const token=generation;working.value=true;error.value='';try{const next=await props.request<NamingPreview>(path,method,body);if(!accept(next,token))throw new Error('命名预览已失效，请重新生成');poll(token);return next.naming as T}finally{if(token===generation&&alive)working.value=false}}
</script>
<template>
  <section class="ingest-naming-preview">
    <button v-if="!engaged" type="button" class="outline wide" :disabled="!available||!input.media||!llmEnabled" @click="create">大模型规范命名</button>
    <p v-if="!engaged" class="modal-note">{{!available?'尚无文件清单，无法提前预览；可直接确认入库，下载完成后自动识别。':!input.media?'关联 TMDB 后可提前核对规范名称。':!llmEnabled?'请在服务连接中启用大模型辅助命名。':'先按规则识别，再用大模型补全缺失信息；预览不会下载、转存或改名。'}}</p>
    <template v-else>
      <p v-if="working||draft?.status==='preparing'||draft?.status==='analyzing'" class="modal-note" role="status">正在生成命名预览…</p>
      <p v-if="error||draft?.error" class="error-text" role="alert">{{error||draft?.error}}</p>
      <TaskNaming v-if="draft?.naming.files.length" :request="previewRequest" :task-id="draft.preview_id" :naming="draft.naming" :llm-enabled="llmEnabled" preview @dirty="dirty=$event"/>
      <button v-else-if="!working&&draft?.status==='failed'" type="button" class="outline wide" @click="create">重新识别</button>
      <p v-if="ready" class="naming-review-note">命名预览已就绪。点击“确认入库”同时确认当前命名方案。</p>
      <button type="button" class="outline wide" @click="cancel">取消命名预览</button>
    </template>
  </section>
</template>
