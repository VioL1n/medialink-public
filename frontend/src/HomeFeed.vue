<script setup lang="ts">
import {computed,onBeforeUnmount,onMounted,ref} from 'vue'
import {ArrowRight,Loader2,RefreshCw} from 'lucide-vue-next'
import MediaImage from './MediaImage.vue'
import HomePosterGrid from './HomePosterGrid.vue'
import {kindLabel,openTarget,updatedLabel} from './homeFeed'
import type {HomeFeed,HomeItem,Media,RequestFn} from './types'
const props=defineProps<{request:RequestFn}>()
const emit=defineEmits<{open:[media:Pick<Media,'type'|'id'>];search:[title:string];settings:[]}>()
const feed=ref<HomeFeed|null>(null), loading=ref(false), refreshing=ref(false), error=ref(''), active=ref(0), paused=ref(false), now=ref(Date.now())
const hero=computed(()=>feed.value?.featured[active.value]||feed.value?.featured[0])
const heroTags=computed(()=>hero.value?hero.value.tags.filter(tag=>tag!==kindLabel(hero.value!)).slice(0,3).join(' / '):'')
const sections=computed(()=>(feed.value?.sections||[]).filter(section=>section.items.length))
const empty=computed(()=>!!feed.value&&!feed.value.featured.length&&!sections.value.length)
let rotation:number|undefined, clock:number|undefined, relink:number|undefined

async function load(){
  loading.value=true;error.value=''
  try{feed.value=await props.request<HomeFeed>('/home');if(active.value>=(feed.value.featured.length||1))active.value=0}catch(e:any){error.value=e.message}finally{loading.value=false}
  if(!feed.value||!empty.value||refreshing.value)return
  // The first local build can take a few minutes; wait for the background pass instead of starting another.
  if(feed.value.refreshing){clearTimeout(relink);relink=window.setTimeout(load,5000)}else refresh()
}
async function refresh(){
  if(refreshing.value)return
  refreshing.value=true;error.value=''
  try{feed.value=await props.request<HomeFeed>('/home/refresh','POST');active.value=0
    // Pick up any later background refresh from the shared cache.
    clearTimeout(relink);relink=window.setTimeout(()=>{if(!refreshing.value)load()},20000)
  }catch(e:any){error.value=e.message}finally{refreshing.value=false}
}
function open(item:HomeItem){const target=openTarget(item);if('media' in target)emit('open',target.media);else emit('search',target.search)}
function openHero(){if(hero.value)open(hero.value)}
function rotate(){if(!paused.value&&feed.value&&feed.value.featured.length>1)active.value=(active.value+1)%feed.value.featured.length}
onMounted(()=>{load();rotation=window.setInterval(rotate,7000);clock=window.setInterval(()=>now.value=Date.now(),60000)})
onBeforeUnmount(()=>{clearInterval(rotation);clearInterval(clock);clearTimeout(relink)})
</script>
<template><div class="home-feed">
    <div v-if="error||feed?.error" class="cinema-notice"><span>{{error||('暂时无法更新：'+feed?.error)}}<template v-if="feed?.updated">，正在显示 {{updatedLabel(feed.updated,now)}} 的内容</template></span><button :disabled="refreshing" @click="refresh">重试</button><button @click="emit('settings')">设置</button></div>
    <div v-else-if="feed?.warnings?.length" class="cinema-notice"><span>部分来源暂时不可用：{{feed.warnings.join('；')}}</span><button :disabled="refreshing" @click="refresh">重试</button></div>
    <div v-if="hero" class="home-hero" @mouseenter="paused=true" @mouseleave="paused=false">
      <button class="home-hero-main" @click="openHero"><MediaImage class="hero-image" :src="hero.backdrop||hero.poster||undefined" :alt="hero.title" eager/><span class="hero-shade"/><span class="home-hero-copy"><span class="cinema-label">{{kindLabel(hero)}}<template v-if="heroTags"> · {{heroTags}}</template></span><strong>{{hero.title}}</strong><em v-if="hero.english_title">{{hero.english_title}}</em><span class="home-hero-overview">{{hero.overview}}</span><span class="cinema-button">查看影片与资源 <ArrowRight :size="17"/></span></span></button>
      <div class="home-featured"><button v-for="(item,index) in feed?.featured" :key="item.id" :class="{active:index===active}" :aria-pressed="index===active" @click="index===active?open(item):active=index"><MediaImage :src="item.poster||undefined" :alt="item.title"/><span><strong>{{item.title}}</strong><small v-if="item.english_title">{{item.english_title}}</small><small>{{item.tags.join(' / ')}}</small></span></button></div>
    </div>
    <section v-for="section in sections" :key="section.key" class="home-section">
      <div class="home-section-heading"><h2>{{section.title}}</h2><span v-if="section===sections[0]" class="home-updated">{{updatedLabel(feed?.updated,now)}}<button class="icon-button" :disabled="refreshing" aria-label="立即更新" @click="refresh"><RefreshCw :size="14" :class="{spin:refreshing}"/></button></span></div>
      <HomePosterGrid :items="section.items" @open="open"/>
    </section>
    <div v-if="(loading||refreshing)&&empty" class="cinema-loading"><Loader2 class="spin" :size="24"/>正在整理最近更新，首次可能需要几分钟</div>
    <div v-else-if="empty&&!error" class="cinema-loading">暂时没有新收录的资源，可在设置中配置 TMDB 并添加 Telegram 频道。</div>
</div></template>
