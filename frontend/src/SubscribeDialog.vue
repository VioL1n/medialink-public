<script setup lang="ts">
import {computed,onMounted,ref,watch} from 'vue'
import {Loader2,Rss,X} from 'lucide-vue-next'
import MediaPicker from './MediaPicker.vue'
import FolderPicker from './FolderPicker.vue'
import {splitWords} from './anime'
import type {AnimeBangumi,MediaSelection,RequestFn,Subscription} from './types'
const props=defineProps<{request:RequestFn;bangumiId:number;title:string}>()
const emit=defineEmits<{close:[];created:[subscription:Subscription]}>()
const detail=ref<AnimeBangumi|null>(null), loading=ref(true), busy=ref(false), error=ref('')
const subgroup=ref<number|null>(null), resolution=ref<string>('1080P'), include=ref(''), exclude=ref('')
const media=ref<MediaSelection|null>(null), mediaTitle=ref(''), target=ref(''), backfill=ref(true), layoutFolder=ref(''), folderEditing=ref(false)
const releases=computed(()=>{
  const groups=detail.value?.subgroups||[]
  const list=subgroup.value===null?groups.flatMap(g=>g.releases):groups.find(g=>g.id===subgroup.value)?.releases||[]
  return [...list].sort((a,b)=>(b.published||'').localeCompare(a.published||'')).slice(0,6)
})
const ready=computed(()=>!!media.value&&(media.value.type==='movie'||media.value.season_number!=null)&&!!target.value.trim()&&!folderEditing.value)

async function load(){
  loading.value=true;error.value=''
  try{
    const data=await props.request<AnimeBangumi>('/anime/bangumi/'+props.bangumiId)
    detail.value=data;target.value=data.target
    // Prefer the group with the most recent releases; "all groups" stays one click away.
    subgroup.value=data.subgroups.find(g=>g.releases.length)?.id??null
    if(data.media){media.value=data.media;try{mediaTitle.value=(await props.request(`/tmdb/${data.media.type}/${data.media.id}`)).title}catch{/* The id is enough. */}}
  }catch(e:any){error.value=e.message}finally{loading.value=false}
}
let layoutGeneration=0
watch(()=>media.value?`${media.value.type}/${media.value.id}`:'',async key=>{const token=++layoutGeneration;layoutFolder.value='';if(!key)return;const [type,id]=key.split('/');try{const data=await props.request<{folder:string|null}>(`/naming/layout?media_type=${type}&media_id=${id}`);if(token===layoutGeneration)layoutFolder.value=data.folder||''}catch{/* The preview line is optional. */}})
async function submit(){
  if(!ready.value)return
  busy.value=true;error.value=''
  try{
    const subscription=await props.request<Subscription>('/subscriptions','POST',{bangumi_id:props.bangumiId,subgroup_id:subgroup.value,media:media.value,target:target.value.trim(),backfill:backfill.value,rules:{resolution:resolution.value||null,include:splitWords(include.value),exclude:splitWords(exclude.value)}})
    emit('created',subscription)
  }catch(e:any){error.value=e.message}finally{busy.value=false}
}
onMounted(load)
</script>
<template><Teleport to="body"><div class="modal-backdrop" @click.self="emit('close')"><form class="modal wide-modal subscribe-modal" @submit.prevent="submit">
  <button type="button" class="modal-close icon-button" aria-label="关闭" @click="emit('close')"><X :size="20"/></button>
  <span class="eyebrow">FOLLOW A SHOW</span><h2>订阅番剧</h2><p class="selected-title">{{detail?.title||title}}</p>
  <div v-if="loading" class="modal-note"><Loader2 class="spin" :size="15"/> 正在读取蜜柑计划的字幕组</div>
  <template v-else-if="detail">
    <p class="modal-note subscribe-facts">{{detail.start?detail.start+' 开播':'开播时间未知'}}<template v-if="detail.bgm_id"> · <a :href="`https://bgm.tv/subject/${detail.bgm_id}`" target="_blank" rel="noopener noreferrer">Bangumi</a></template></p>
    <div class="subscribe-groups" role="radiogroup" aria-label="字幕组"><button type="button" class="filter-chip" :class="{active:subgroup===null}" @click="subgroup=null">全部字幕组</button><button v-for="g in detail.subgroups" :key="g.id" type="button" class="filter-chip" :class="{active:subgroup===g.id}" @click="subgroup=g.id">{{g.name}}</button></div>
    <ul v-if="releases.length" class="subscribe-releases"><li v-for="r in releases" :key="r.hash"><span>{{r.episode!=null?'第 '+r.episode+' 集':'不入库'}}</span>{{r.title}}</li></ul>
    <p v-else class="modal-note">这个字幕组暂时没有发布，订阅后会在发布时入库。</p>
    <div class="subscribe-rules"><label>清晰度<select v-model="resolution" aria-label="订阅清晰度"><option value="1080P">1080P</option><option value="2160P">2160P</option><option value="720P">720P</option><option value="">不限</option></select></label><label>必须包含<input v-model="include" placeholder="如 简体、CHS" aria-label="必须包含的关键词"/></label><label>排除<input v-model="exclude" placeholder="如 繁体、HEVC" aria-label="要排除的关键词"/></label></div>
    <MediaPicker :request="request" v-model="media" v-model:title="mediaTitle" :disabled="busy"/>
    <p v-if="media?.type==='tv'&&media.season_number==null" class="error-text">请填写对应的 TMDB 季数，新集会放进这一季。</p>
    <label>入库分类目录<input v-model="target" aria-label="订阅入库目录" required/></label>
    <p v-if="layoutFolder" class="modal-note layout-preview">将放入：{{target.replace(/\/$/,'')}}/{{layoutFolder}}{{media?.type==='tv'?'/Season '+String(media.season_number??'').padStart(2,'0'):''}}</p>
    <FolderPicker :request="request" v-model="target" :disabled="busy" @editing="folderEditing=$event"/>
    <label class="auto-naming-option"><input v-model="backfill" type="checkbox"/>补齐已播出集数</label>
    <p class="modal-note">每 30 分钟检查一次蜜柑计划 RSS。每集只入库一个符合规则的版本，自动筛掉广告文件并按 TMDB 规范命名；无法确定集数时停在入库任务等待处理。</p>
  </template>
  <div class="modal-footer"><p v-if="error" class="error-text">{{error}}</p>
  <button class="primary wide" :disabled="busy||loading||!ready"><Loader2 v-if="busy" class="spin" :size="17"/><Rss v-else :size="17"/>{{busy?'正在订阅并检查':'订阅并自动入库'}}</button></div>
</form></div></Teleport></template>
