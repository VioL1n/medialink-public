<script setup lang="ts">
import {computed,onUnmounted,ref,watch} from 'vue'
import {Loader2,RefreshCw} from 'lucide-vue-next'
import MagnetTree from './MagnetTree.vue'
import {defaultMagnetFiles,fileBytes,magnetTree,toggleFiles} from './magnetTree'
import type {MagnetPreview,MagnetSelection,RequestFn} from './types'
const props=defineProps<{link:string;request:RequestFn;modelValue:MagnetSelection|null;disabled?:boolean;autoNaming?:boolean}>()
const emit=defineEmits<{'update:modelValue':[selection:MagnetSelection|null];ready:[ready:boolean]}>()
const preview=ref<MagnetPreview|null>(null), chosen=ref<string[]>([]), loading=ref(false), error=ref('')
const selected=computed(()=>new Set(chosen.value))
const selectedFiles=computed(()=>preview.value?.files.filter(file=>selected.value.has(file.id))||[])
const trees=computed(()=>magnetTree(preview.value?.files||[]))
const size=computed(()=>selectedFiles.value.reduce((total,file)=>total+file.size,0))
const MAX_WAIT=200000, elapsed=ref(0)
let generation=0, controller:AbortController|undefined, timer:number|undefined
function stopTimer(){if(timer!==undefined)window.clearInterval(timer);timer=undefined}
// Previewing is optional: without a list the whole download is curated after it reaches 115.
function publish(){
  emit('update:modelValue',preview.value?{preview_id:preview.value.preview_id,selected_ids:[...chosen.value]}:null)
  emit('ready',!preview.value||selectedFiles.value.some(file=>file.kind==='video'))
}
async function load(){
  const token=++generation;controller?.abort();controller=new AbortController()
  preview.value=null;chosen.value=[];error.value='';loading.value=true;publish()
  const started=Date.now();elapsed.value=0;stopTimer();timer=window.setInterval(()=>{elapsed.value=Math.round((Date.now()-started)/1000)},1000)
  try{
    let result:MagnetPreview
    // The server keeps resolving after a request times out; asking again resumes it.
    for(;;){
      try{result=await props.request<MagnetPreview>('/magnets/preview','POST',{link:props.link},controller.signal);break}
      catch(e:any){if(token!==generation||!e.retry||Date.now()-started>MAX_WAIT)throw e}
    }
    if(token!==generation)return
    preview.value=result;chosen.value=defaultMagnetFiles(result.files)
  }catch(e:any){if(token===generation)error.value=e.message||'磁力列表解析失败'}
  finally{if(token===generation){stopTimer();loading.value=false;publish()}}
}
function selectAll(){chosen.value=preview.value?.files.map(file=>file.id)||[]}
watch(chosen,publish,{deep:true})
watch(()=>props.link,load,{immediate:true})
onUnmounted(()=>{generation++;controller?.abort();stopTimer()})
</script>
<template>
  <section class="magnet-picker" aria-label="磁力文件预览">
    <b>磁力内的文件</b>
    <p v-if="loading" class="modal-note" role="status"><Loader2 class="spin" :size="15"/>正在解析文件列表{{elapsed>=5?`（已 ${elapsed} 秒）`:''}}，可直接确认入库…</p>
    <template v-else-if="error"><p class="error-text" role="alert">{{error}}</p><div class="magnet-controls"><button type="button" class="outline" :disabled="disabled" @click="load"><RefreshCw :size="14"/>重试解析</button></div></template>
    <p v-if="!preview" class="modal-note">文件列表只用于提前查看和调整。{{autoNaming?'不等待也可以确认：115 下载完成后会按实际文件自动筛掉广告和无关文件。':'未勾选自动规范命名时，将整包按原名入库。'}}</p>
    <template v-if="preview">
      <div class="magnet-controls"><button type="button" class="text-button" :disabled="disabled" @click="selectAll">全选</button><button type="button" class="text-button" :disabled="disabled" @click="chosen=[]">清空</button><button type="button" class="text-button" :disabled="disabled" @click="chosen=defaultMagnetFiles(preview.files)">推荐选择</button></div>
      <p class="magnet-summary" role="status">已选 {{selectedFiles.length}} / {{preview.files.length}} 个文件 · {{fileBytes(size)}}</p>
      <ul class="magnet-tree"><MagnetTree v-for="node in trees" :key="node.path" :node="node" :chosen="selected" :disabled="disabled" @toggle="(ids,checked)=>chosen=toggleFiles(chosen,ids,checked)"/></ul>
      <p v-if="!selectedFiles.some(file=>file.kind==='video')" class="modal-note">请至少勾选一个视频文件。</p>
      <p class="modal-note">已默认排除广告、样片和非影视文件。确认后 115 会整包下载，仅勾选文件进入媒体库，其余保留在暂存目录。</p>
    </template>
  </section>
</template>
