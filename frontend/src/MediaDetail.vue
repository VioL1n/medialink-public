<script setup lang="ts">
import {ref,watch} from 'vue'
import {ArrowLeft,Star,Loader2,Film} from 'lucide-vue-next'
import MediaImage from './MediaImage.vue'
import ResourceSearch from './ResourceSearch.vue'
import type {Media,RequestFn,Resource,SearchSource} from './types'
const props=defineProps<{request:RequestFn;type:'movie'|'tv';id:number;sources:SearchSource[];configured:boolean}>()
const emit=defineEmits<{choose:[resource:Resource];open:[media:Media];back:[];settings:[];unauthorized:[]}>()
const media=ref<Media|null>(null), loading=ref(false), error=ref(''), season=ref<string>('all')
let generation=0
async function load(){const token=++generation;media.value=null;season.value='all';loading.value=true;error.value='';try{const data=await props.request(`/tmdb/${props.type}/${props.id}`);if(token===generation)media.value=data}catch(e:any){if(token===generation)error.value=e.message}finally{if(token===generation)loading.value=false}}
watch(()=>[props.type,props.id],load,{immediate:true})
</script>
<template><section class="cinema-page media-detail">
  <button class="cinema-back" @click="emit('back')"><ArrowLeft :size="17"/>返回海报墙</button>
  <div v-if="loading" class="cinema-loading"><Loader2 class="spin" :size="26"/>正在载入影片资料</div>
  <div v-else-if="error" class="cinema-setup"><Film :size="32"/><h2>影片资料暂不可用</h2><p>{{error}}</p><button class="cinema-button" @click="load">重新加载</button><button class="cinema-button secondary" @click="emit('settings')">设置</button></div>
  <template v-else-if="media">
    <div class="detail-hero"><MediaImage class="detail-backdrop" :src="media.backdrop" :alt="media.title" eager/><div class="detail-shade"/><div class="detail-summary"><MediaImage class="detail-poster" :src="media.poster" :alt="media.title" eager/><div class="detail-copy"><span class="cinema-label">{{media.type==='movie'?'电影':'剧集'}} · {{media.year||'年份未知'}}</span><h1>{{media.title}}</h1><p v-if="media.english_title&&media.english_title!==media.title" class="detail-english">{{media.english_title}}</p><p v-if="media.original_title&&media.original_title!==media.title&&media.original_title!==media.english_title" class="detail-original">原名：{{media.original_title}}</p><div class="detail-facts"><span v-if="media.rating"><Star :size="16"/>{{media.rating.toFixed(1)}} <small>{{(media.votes||0).toLocaleString()}} 人评分</small></span><span v-else>评分未知</span><span>{{media.type==='movie'?(media.runtime?media.runtime+' 分钟':'时长未知'):(media.season_count!=null?media.season_count+' 季':'季数未知')+' · '+(media.episode_count!=null?media.episode_count+' 集':'集数未知')}}</span><span v-for="genre in media.genres" :key="genre">{{genre}}</span></div><p class="detail-overview"><small v-if="media.overview_language==='en'">英文简介</small>{{media.overview||'暂时没有影片简介。'}}</p><p class="detail-creators">{{media.type==='movie'?'导演':'主创'}}：{{media.creators?.join(' ／ ')||'未知'}}</p><p v-if="media.stale" class="detail-cache">缓存资料 · {{new Date((media.updated||0)*1000).toLocaleString('zh-CN')}}</p><p v-if="media.warning" class="detail-cache">{{media.warning}}</p></div></div></div>
    <div v-if="media.cast?.length" class="cast-section"><h2>主要演员</h2><div class="cast-strip"><article v-for="person in media.cast" :key="person.name+person.character"><MediaImage :src="person.portrait" :alt="person.name"/><h3>{{person.name}}</h3><p>{{person.character||'角色未知'}}</p></article></div></div>
    <div class="detail-resource-heading"><div><span class="eyebrow">FIND YOUR COPY</span><h2>找到这部{{media.type==='movie'?'电影':'剧集'}}的资源</h2><p>中英文片名聚合搜索，选中资源送入你的媒体库。</p></div><label v-if="media.type==='tv'" class="season-selector">搜索范围<select v-model="season" aria-label="剧集季数"><option value="all">整部剧集</option><option v-for="s in media.seasons" :key="s.number" :value="String(s.number)">{{s.number===0?'特别篇':'第 '+s.number+' 季'}} · {{s.episodes||0}} 集</option></select></label></div>
    <ResourceSearch :request="request" :sources="sources" :configured="configured" :media="media" :season="season==='all'?undefined:Number(season)" auto dark @choose="emit('choose',$event)" @open="emit('open',$event)" @settings="emit('settings')" @unauthorized="emit('unauthorized')"/>
    <p class="resource-disclaimer">资源匹配供选择时参考，请核对版本、年份和季数后入库。</p>
  </template>
</section></template>
