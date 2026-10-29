<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {Check,Loader2,Rss,RefreshCw} from 'lucide-vue-next'
import MediaImage from './MediaImage.vue'
import SubscribeDialog from './SubscribeDialog.vue'
import {defaultDay,orderedDays} from './anime'
import {updatedLabel} from './homeFeed'
import type {AnimeItem,AnimeSchedule,Media,RequestFn,Subscription} from './types'
const props=defineProps<{request:RequestFn;enabled:boolean}>()
const emit=defineEmits<{open:[media:Pick<Media,'type'|'id'>];search:[title:string];subscriptions:[]}>()
const data=ref<AnimeSchedule|null>(null), loading=ref(false), error=ref(''), day=ref<number|null>(null), season=ref('')
const subscribing=ref<AnimeItem|null>(null), notice=ref('')
const days=computed(()=>orderedDays(data.value?.days||[]))
const items=computed(()=>days.value.find(d=>d.weekday===day.value)?.items||[])
let generation=0, relink:number|undefined, relinks=0

function query(){if(!season.value)return '';const [year,name]=season.value.split('-');return `?year=${year}&season=${encodeURIComponent(name)}`}
async function load(refresh=false){
  const token=++generation;loading.value=true;error.value=''
  try{
    const next=await props.request<AnimeSchedule>('/anime/schedule'+(refresh?'/refresh':'')+query(),refresh?'POST':'GET')
    if(token!==generation)return
    data.value=next
    if(!season.value&&next.year)season.value=`${next.year}-${next.season}`
    if(day.value===null||!next.days.some(d=>d.weekday===day.value&&d.items.length))day.value=defaultDay(next.days)
    // TMDB links resolve in the background after a fetch; pick them up a few times while unlinked titles remain.
    clearTimeout(relink)
    if(relinks<4&&next.days.some(d=>d.items.some(i=>!i.media)))relink=window.setTimeout(()=>{relinks++;if(props.enabled)load()},30000)
  }catch(e:any){if(token===generation)error.value=e.message}finally{if(token===generation)loading.value=false}
}
function open(item:AnimeItem){if(item.media)emit('open',item.media);else emit('search',item.title)}
function changeSeason(value:string){season.value=value;day.value=null;relinks=0;load()}
function subscribed(subscription:Subscription){
  const item=subscribing.value;subscribing.value=null
  if(item)item.subscribed=true
  const queued=subscription.episodes.filter(e=>e.task_id).length
  notice.value=subscription.error?`已订阅《${subscription.title}》，但首次检查失败：${subscription.error}`
    :queued?`已订阅《${subscription.title}》，${queued} 集已加入入库任务`:`已订阅《${subscription.title}》，新集发布后会自动入库`
}
onBeforeUnmount(()=>clearTimeout(relink))
watch(()=>props.enabled,value=>{if(value&&!data.value&&!loading.value)load()},{immediate:true})
</script>
<template><div class="anime-schedule">
  <div class="anime-toolbar">
    <div class="filter-options anime-days" role="tablist" aria-label="放送星期"><button v-for="d in days" :key="d.weekday" class="filter-chip" :class="{active:d.weekday===day}" role="tab" :aria-selected="d.weekday===day" @click="day=d.weekday">{{d.label}}<small>{{d.items.length}}</small></button></div>
    <div class="anime-season"><select v-if="data?.seasons.length" :value="season" aria-label="番剧季度" @change="changeSeason(($event.target as HTMLSelectElement).value)"><option v-for="s in data.seasons" :key="s.year+s.season" :value="`${s.year}-${s.season}`">{{s.year}} {{s.season}}季</option></select><button class="icon-button" :disabled="loading" aria-label="从蜜柑计划刷新" @click="load(true)"><RefreshCw :size="15" :class="{spin:loading}"/></button></div>
  </div>
  <div v-if="notice" class="cinema-notice anime-notice"><span>{{notice}}</span><button @click="emit('subscriptions')">查看追番订阅</button><button @click="notice=''">关闭</button></div>
  <div v-if="error||data?.error" class="cinema-notice"><span>{{error||('暂时无法更新：'+data?.error)}}<template v-if="data?.updated">，正在显示 {{updatedLabel(data.updated,Date.now())}} 的内容</template></span><button :disabled="loading" @click="load(true)">重试</button></div>
  <div v-if="items.length" class="poster-grid anime-grid">
    <div v-for="item in items" :key="item.id" class="anime-card">
      <button class="poster-card" :title="item.title" @click="open(item)"><div class="poster-cover"><MediaImage :src="item.poster||undefined" :alt="item.title"/><span v-if="item.subscribed" class="anime-badge"><Check :size="11"/>已订阅</span></div><h3>{{item.title}}</h3><p><span>{{item.updated?item.updated.slice(5).replace('-','/')+' 更新':'暂无更新'}}</span><span>{{item.media?'TMDB':'搜索资源'}}</span></p></button>
      <button class="anime-subscribe" :class="{done:item.subscribed}" @click="subscribing=item"><Rss :size="13"/>{{item.subscribed?'再订阅其他字幕组':'订阅'}}</button>
    </div>
  </div>
  <div v-else-if="loading" class="cinema-loading"><Loader2 class="spin" :size="24"/>正在从蜜柑计划获取番表</div>
  <div v-else-if="data&&!error" class="cinema-loading">这一天没有放送的番剧</div>
  <SubscribeDialog v-if="subscribing" :request="request" :bangumi-id="subscribing.id" :title="subscribing.title" @close="subscribing=null" @created="subscribed"/>
</div></template>
