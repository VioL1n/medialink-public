<script setup lang="ts">
import {computed,onBeforeUnmount,onMounted,ref,watch} from 'vue'
import {Search,ArrowRight,Loader2,Check,AlertCircle,Film,Link,Plus,ArrowUpRight,X,RefreshCw} from 'lucide-vue-next'
import MediaImage from './MediaImage.vue'
import {mergeResourceResults} from './resourceOrder'
import {reconcileSourceSelection} from './settingsState'
import type {Media,RequestFn,Resource,SearchSource} from './types'
const props=defineProps<{request:RequestFn;sources:SearchSource[];configured:boolean;defaultUseTmdb?:boolean;media?:Media;season?:number;auto?:boolean;dark?:boolean;initial?:string}>()
const emit=defineEmits<{choose:[resource:Resource];open:[media:Media];settings:[];unauthorized:[]}>()
const query=ref(props.media?.title||props.initial||''), selection=ref<string[]>(props.sources.map(s=>s.id)), useTmdb=ref(props.defaultUseTmdb??true)
const results=ref<Resource[]>([]), searching=ref(false), searched=ref(false), error=ref('')
const statuses=ref<Record<string,string>>({}), failures=ref<Record<string,string>>({}), counts=ref<Record<string,number>>({})
const kind=ref('all'), quality=ref('all'), provider=ref('all'), metadata=ref<any>(null), chosen=ref<Media|null>(null)
const showCandidates=ref(false), candidateMore=ref(false), candidatePool=ref<Media[]>([])
let stream:EventSource|null=null, generation=0
const name=(id:string)=>props.sources.find(s=>s.id===id)?.name||id
const visible=computed(()=>results.value.filter(r=>(kind.value==='all'||r.kind===kind.value)&&(quality.value==='all'||r.resolution===quality.value)&&(provider.value==='all'||r.sources.some(s=>s.provider===provider.value))))
const candidates=computed(()=>candidateMore.value?candidatePool.value:candidatePool.value.slice(0,6))
const limeEmpty=computed(()=>statuses.value.limetorrents==='done'&&counts.value.limetorrents===0&&!failures.value.limetorrents)
function stop(){generation++;stream?.close();stream=null;searching.value=false}
function merge(incoming:Resource[],final=false){results.value=mergeResourceResults(results.value,incoming,final)}

async function search(media:Media|null=chosen.value){
  const title=props.media?.title||query.value.trim()
  if(!title||!selection.value.length)return
  stop();const token=generation
  if(!media&&!props.media)candidatePool.value=[]
  results.value=[];metadata.value=null;failures.value={};counts.value={};error.value='';searched.value=true;searching.value=true;showCandidates.value=false;candidateMore.value=false
  statuses.value=Object.fromEntries(selection.value.map(id=>[id,'loading']))
  try{
    const item=props.media||media
    const job=await props.request('/search','POST',{query:title,sources:selection.value,use_tmdb:useTmdb.value,media:item?{type:item.type,id:item.id,...(props.season!==undefined?{season_number:props.season}:{})}:undefined})
    if(token!==generation)return
    stream=new EventSource('/api/v1/search/'+job.id+'/events')
    stream.addEventListener('metadata',(event:any)=>{if(token!==generation)return;metadata.value=JSON.parse(event.data);if(metadata.value.candidates?.length)candidatePool.value=metadata.value.candidates;showCandidates.value=metadata.value.status==='candidates'})
    stream.addEventListener('result',(event:any)=>{if(token!==generation)return;const data=JSON.parse(event.data);statuses.value[data.provider]=data.status;counts.value[data.provider]=data.results.length;if(data.error)failures.value[data.provider]=data.error;merge(data.results)})
    stream.addEventListener('done',(event:any)=>{if(token!==generation)return;merge(JSON.parse(event.data).results,true);searching.value=false;stream?.close()})
    stream.addEventListener('expired',()=>{if(token!==generation)return;stop();error.value='搜索等待超时，已取得的结果保留'})
    stream.addEventListener('auth_required',()=>{if(token!==generation)return;stop();emit('unauthorized')})
    stream.onerror=async()=>{if(token!==generation)return;stop();const stopped=generation;error.value='搜索连接中断，已取得的结果保留，可重新搜索';try{const state=await props.request('/auth/state');if(stopped===generation&&!state.authenticated)emit('unauthorized')}catch{}}
  }catch(e:any){if(token===generation){searching.value=false;error.value=e.message}}
}
function chooseResource(resource:Resource){const item=props.media||(metadata.value?.status==='resolved'?metadata.value.media:null);emit('choose',{...resource,...(item?{media:{type:item.type,id:item.id,season_number:props.season??metadata.value?.season_number}}:{})})}
function selectMedia(media:Media){chosen.value=media;useTmdb.value=true;search(media)}
function changeQuery(){chosen.value=null}
function disableAssist(){chosen.value=null;useTmdb.value=false;search(null)}
function toggleAssist(){if(!useTmdb.value){chosen.value=null;if(searched.value)search(null)}}
watch(()=>props.sources.map(s=>s.id).join(','),(next,previous)=>{
  selection.value=reconcileSourceSelection(previous?previous.split(','):[],selection.value,next?next.split(','):[])
  stop();results.value=[];metadata.value=null;statuses.value={};failures.value={};counts.value={};error.value='';searched.value=false;provider.value='all'
  if(props.auto&&props.media&&selection.value.length)search(props.media)
})
watch(()=>props.defaultUseTmdb,value=>{if(!props.media&&!chosen.value)useTmdb.value=value??true})
watch(()=>[props.media?.type,props.media?.id,props.season],()=>{if(props.auto&&props.media){query.value=props.media.title;search(props.media)}},{immediate:true})
onMounted(()=>{if(props.initial&&!props.media)search(null)})
onBeforeUnmount(stop)
</script>
<template>
  <section class="resource-search" :class="{'resource-search-dark':dark}">
    <div class="search-panel"><form @submit.prevent="search()"><Search :size="23"/><input v-model="query" :readonly="!!media" @input="changeQuery" placeholder="搜索片名、剧集、年份或资源关键词…" aria-label="影视资源关键词"/><button class="primary" :disabled="searching||!query.trim()||!selection.length"><Loader2 v-if="searching" class="spin" :size="18"/>{{searching?'正在搜索':'搜索资源'}}<ArrowRight v-if="!searching" :size="17"/></button></form><div class="source-toggles"><span>搜索来源</span><label v-for="source in sources" :key="source.id"><input v-model="selection" type="checkbox" :value="source.id"/><span>{{source.name}}</span><Loader2 v-if="statuses[source.id]==='loading'" class="spin" :size="13"/><Check v-if="statuses[source.id]==='done'" :size="13"/></label></div><label v-if="!media" class="tmdb-assist-toggle"><input v-model="useTmdb" aria-label="TMDB 片名辅助" type="checkbox" @change="toggleAssist" :disabled="!configured"/><span>TMDB 片名辅助</span><button v-if="!configured" type="button" class="text-button" @click="emit('settings')">配置 TMDB</button><small v-else>明确匹配时补充中英文名，同名影片先选择</small></label></div>
    <div v-if="metadata?.status==='resolved'" class="identified-media"><MediaImage :src="metadata.media.poster" :alt="metadata.media.title"/><div><small>已识别影片 · {{metadata.media.type==='movie'?'电影':'剧集'}}</small><button class="identified-title" @click="emit('open',metadata.media)">{{metadata.media.title}} <span>{{metadata.media.year||'年份未知'}}</span><ArrowUpRight :size="14"/></button><p>搜索词：{{metadata.queries.join(' ／ ')}}</p><small v-if="metadata.stale">缓存资料 · {{new Date(metadata.updated*1000).toLocaleString('zh-CN')}}</small></div><div v-if="!media" class="assist-actions"><button class="text-button" @click="showCandidates=!showCandidates">更换影片</button><button class="text-button" @click="disableAssist">仅搜原词</button></div></div>
    <div v-else-if="metadata?.message&&metadata.status!=='unconfigured'" class="banner coverage"><Film :size="17"/><span>{{metadata.message}}</span></div>
    <div v-if="showCandidates" class="candidate-area"><div class="candidate-heading"><span>选择影片，补充中英文资源搜索</span><button class="icon-button" aria-label="关闭影片候选" @click="showCandidates=false"><X :size="15"/></button></div><div class="candidate-grid"><button v-for="item in candidates" :key="item.type+item.id" class="candidate-card" @click="selectMedia(item)"><MediaImage :src="item.poster" :alt="item.title"/><span><b>{{item.title}}</b><small>{{item.type==='movie'?'电影':'剧集'}} · {{item.year||'年份未知'}}</small><small>{{item.english_title||item.original_title}}</small></span></button></div><button v-if="candidatePool.length>6&&!candidateMore" class="text-button" @click="candidateMore=true">显示更多候选</button><p v-if="!candidates.length">没有其他候选，可修改关键词重新搜索。</p></div>
    <div v-for="(message,id) in failures" :key="id" class="banner error"><AlertCircle :size="17"/><span>{{name(String(id))}}：{{message}}</span></div>
    <div v-if="error" class="banner error"><AlertCircle :size="17"/><span>{{error}}</span><button class="text-button" @click="search()">重试</button></div>
    <div v-if="limeEmpty" class="banner coverage"><Search :size="17"/><span>LimeTorrents：未找到匹配资源，可尝试英文片名。</span></div>
    <div class="results-heading"><div class="resource-order-heading"><h2>资源搜索 <span v-if="searched">{{visible.length}}</span></h2><p v-if="searched" class="resource-order-note">清晰度优先 · 2160P 与 4K 同档</p></div><div v-if="searched" class="filters"><select v-model="kind" aria-label="资源类型"><option value="all">全部类型</option><option value="magnet">磁力链接</option><option value="share">115 分享</option></select><select v-model="quality" aria-label="资源清晰度"><option value="all">全部清晰度</option><option value="2160P">2160P</option><option value="1080P">1080P</option><option value="720P">720P</option><option value="4K">4K</option></select><select v-model="provider" aria-label="资源来源"><option value="all">全部来源</option><option v-for="source in sources" :key="source.id" :value="source.id">{{source.name}}</option></select></div></div>
    <div v-if="visible.length" class="results-list"><article v-for="r in visible" :key="r.key" class="resource"><div class="resource-icon"><Film v-if="r.kind==='magnet'" :size="23"/><Link v-else :size="23"/></div><div class="resource-body"><h3>{{r.title}}</h3><div class="resource-tags"><span class="kind-tag">{{r.kind==='magnet'?'磁力':'115 分享'}}</span><span v-if="r.resolution" class="quality-tag">{{r.resolution}}</span><span>{{r.size||'大小未知'}}</span><span>{{r.published?new Date(r.published).toLocaleDateString('zh-CN'):'时间未知'}}</span><span v-if="r.seeders">{{r.seeders}} 做种</span><span v-if="r.match" class="match-tag" :class="r.match.status" :title="r.match.explanation">{{r.match.status==='matched'?'已匹配':'待核对'}}</span></div><p v-if="r.match" class="match-explanation">{{r.match.explanation}}</p><div class="resource-sources"><a v-for="s in r.sources" :key="s.provider+s.url" :href="s.url" target="_blank" rel="noopener noreferrer">{{s.name}}<ArrowUpRight :size="12"/></a></div></div><button class="resource-add" @click="chooseResource(r)"><Plus :size="16"/>入库 115</button></article></div>
    <div v-else-if="!sources.length" class="empty-state"><Search :size="32"/><h3>尚未启用搜索源</h3><p>在设置中启用来源后，即可搜索影视资源。</p><button class="outline" @click="emit('settings')">配置搜索源 <ArrowRight :size="15"/></button></div>
    <div v-else class="empty-state"><Loader2 v-if="searching" class="spin" :size="30"/><Search v-else :size="32"/><h3>{{searching?'正在汇聚资源…':searched?'暂时没有匹配资源':'从一个片名开始'}}</h3><p>{{searched?'可以更换片名或来源后重试。':'输入片名，或从发现页面选择影片。'}}</p></div>
    <button v-if="media&&searched&&!searching" class="outline resource-retry" @click="search(media)"><RefreshCw :size="15"/>重新搜索资源</button>
  </section>
</template>
