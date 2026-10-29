<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {ArrowRight,Loader2,RotateCcw} from 'lucide-vue-next'
import PosterWall from './PosterWall.vue'
import type {Media,RequestFn} from './types'
type Option={id:string;name:string}
type Genre={id:number;name:string}
type Options={genres:Genre[];countries:Option[];languages:Option[];stale:boolean;updated:number}
const props=defineProps<{request:RequestFn;enabled:boolean}>()
const emit=defineEmits<{open:[media:Media];settings:[]}>()
const mediaType=ref('movie'), options=ref<Options|null>(null), optionsError=ref(''), notice=ref('')
const included=ref<number[]>([]),excluded=ref<number[]>([]),genreMode=ref('include')
const country=ref(''),language=ref(''),yearFrom=ref(''),yearTo=ref(''),yearChoice=ref('all'),draftFrom=ref(''),draftTo=ref(''),yearError=ref('')
const expanded=ref<Record<string,boolean>>({})
const items=ref<Media[]>([]),page=ref(0),total=ref(1),loading=ref(false),error=ref(''),stale=ref(false),updated=ref(0)
let generation=0,optionGeneration=0,timer:ReturnType<typeof setTimeout>|undefined,initialized=false
const now=new Date().getFullYear()
const years=[{id:'all',name:'全部',from:'',to:''},{id:'recent',name:'近两年',from:String(now-1),to:String(now)},...Array.from({length:10},(_,i)=>({id:String(now-i),name:String(now-i),from:String(now-i),to:String(now-i)})),...[2020,2010,2000,1990,1980,1970,1960].map(y=>({id:`decade${y}`,name:`${String(y).slice(2)}年代`,from:String(y),to:String(y+9)})),{id:'earlier',name:'更早',from:'',to:'1959'}]
function order(values:Option[],priority:string[]){return [...values].sort((a,b)=>{const ai=priority.indexOf(a.id),bi=priority.indexOf(b.id);return (ai<0?999:ai)-(bi<0?999:bi)||a.name.localeCompare(b.name,'zh-CN')})}
const countries=computed(()=>order(options.value?.countries||[],['CN','HK','TW','US','JP','KR','GB','FR','DE','IN','TH','CA','RU','IT','ES','AU']))
const languages=computed(()=>order(options.value?.languages||[],['zh','cn','en','ja','ko','fr','de','es','th','ru','it','hi','xx']))
const selected=computed(()=>[
 ...included.value.map(id=>`包含 ${options.value?.genres.find(g=>g.id===id)?.name||id}`),
 ...excluded.value.map(id=>`排除 ${options.value?.genres.find(g=>g.id===id)?.name||id}`),
 ...(yearFrom.value||yearTo.value?[`${yearFrom.value||'更早'}—${yearTo.value||'至今'}年`]:[]),
 ...(country.value?[countries.value.find(c=>c.id===country.value)?.name||country.value]:[]),
 ...(language.value?[`原始语言：${languages.value.find(l=>l.id===language.value)?.name||language.value}`]:[])
])
function toggleGenre(id:number){
 const target=genreMode.value==='include'?included:excluded,other=genreMode.value==='include'?excluded:included
 target.value=target.value.includes(id)?target.value.filter(v=>v!==id):[...target.value,id]
 other.value=other.value.filter(v=>v!==id)
}
function setYear(id:string){const entry=years.find(y=>y.id===id);if(!entry)return;yearChoice.value=id;yearFrom.value=draftFrom.value=entry.from;yearTo.value=draftTo.value=entry.to;yearError.value=''}
function applyYears(){
 const from=draftFrom.value.trim(),to=draftTo.value.trim()
 if([from,to].some(v=>v&&(!/^\d{1,4}$/.test(v)||Number(v)<1000))||(from&&to&&Number(from)>Number(to))){yearError.value='请输入 1000 至 9999 之间的年份，起始年份不能晚于结束年份';return}
 yearError.value='';yearChoice.value=from||to?'custom':'all';yearFrom.value=from;yearTo.value=to
}
function reset(){genreMode.value='include';included.value=[];excluded.value=[];country.value='';language.value='';setYear('all');notice.value=''}
function schedule(){
 clearTimeout(timer);generation++;loading.value=true;items.value=[];page.value=0;error.value=''
 if(!props.enabled||!options.value){loading.value=false;return}
 timer=setTimeout(()=>loadMore(true),250)
}
async function loadMore(reset=false){
 if(!props.enabled||!options.value)return
 if(loading.value&&!reset)return
 clearTimeout(timer);const token=++generation;loading.value=true;error.value=''
 if(reset){items.value=[];page.value=0}
 const params=new URLSearchParams({media_type:mediaType.value,page:String(page.value+1)})
 if(included.value.length)params.set('include_genres',included.value.join(','))
 if(excluded.value.length)params.set('exclude_genres',excluded.value.join(','))
 if(yearFrom.value)params.set('year_from',yearFrom.value)
 if(yearTo.value)params.set('year_to',yearTo.value)
 if(country.value)params.set('country',country.value)
 if(language.value)params.set('original_language',language.value)
 try{const data=await props.request(`/tmdb/discover?${params}`);if(token!==generation)return;const known=new Set(items.value.map(i=>i.type+i.id));items.value.push(...data.items.filter((i:Media)=>!known.has(i.type+i.id)));page.value=data.page;total.value=data.total_pages;stale.value=data.stale;updated.value=data.updated}
 catch(e:any){if(token===generation)error.value=e.message}
 finally{if(token===generation)loading.value=false}
}
async function loadOptions(){
 clearTimeout(timer);generation++;const token=++optionGeneration;options.value=null;optionsError.value='';loading.value=true;items.value=[];page.value=0
 try{
  const result:Options=await props.request(`/tmdb/filters?media_type=${mediaType.value}`)
  if(token!==optionGeneration||!props.enabled)return
  const allowed=new Set(result.genres.map(g=>g.id)),removed=[...included.value,...excluded.value].filter(id=>!allowed.has(id))
  included.value=included.value.filter(id=>allowed.has(id));excluded.value=excluded.value.filter(id=>allowed.has(id))
  notice.value=removed.length?'已清除当前影视类别不支持的类型，其余筛选已保留。':''
  options.value=result;schedule()
 }catch(e:any){if(token===optionGeneration){optionsError.value=e.message;loading.value=false}}
}
watch([included,excluded,country,language,yearFrom,yearTo],()=>{if(options.value)schedule()})
watch(mediaType,()=>{if(props.enabled)loadOptions()})
watch(()=>props.enabled,value=>{
 if(value){if(!initialized||!options.value){initialized=true;loadOptions()}else if(!page.value) schedule()}
 else{clearTimeout(timer);generation++;optionGeneration++;loading.value=false}
},{immediate:true})
onBeforeUnmount(()=>{clearTimeout(timer);generation++;optionGeneration++})
</script>
<template>
<div class="category-browse">
 <div class="discovery-toolbar"><div class="cinema-tabs"><button v-for="entry in [{id:'movie',name:'电影'},{id:'tv',name:'剧集'}]" :key="entry.id" :class="{active:mediaType===entry.id}" @click="mediaType=entry.id">{{entry.name}}</button></div><span class="browse-sort">按热度排序</span></div>
 <div v-if="optionsError" class="cinema-notice"><span>{{optionsError}}</span><button @click="loadOptions">重试</button><button @click="emit('settings')">连接设置</button></div>
 <template v-if="options">
 <div class="browse-filter-panel">
  <div class="browse-filter-row"><span class="filter-label">类型</span><div class="filter-options" :class="{expanded:expanded.genres}"><button class="filter-chip" :class="{active:!included.length&&!excluded.length}" @click="included=[];excluded=[]">全部</button><button v-for="g in options.genres" :key="g.id" class="filter-chip" :class="{active:included.includes(g.id),excluded:excluded.includes(g.id)}" :aria-pressed="included.includes(g.id)||excluded.includes(g.id)" @click="toggleGenre(g.id)">{{excluded.includes(g.id)?'− ':''}}{{g.name}}</button></div><button class="filter-more mobile-more" :aria-expanded="!!expanded.genres" @click="expanded.genres=!expanded.genres">{{expanded.genres?'收起':'更多'}}</button></div>
  <div class="genre-mode"><span>同时满足所选类型</span><button :class="{active:genreMode==='include'}" @click="genreMode='include'">多选包含</button><button :class="{active:genreMode==='exclude'}" @click="genreMode='exclude'">排除类型</button></div>
  <div class="browse-filter-row"><span class="filter-label">年代</span><div class="filter-options" :class="{expanded:expanded.years}"><button v-for="y in years" :key="y.id" class="filter-chip" :class="{active:yearChoice===y.id}" @click="setYear(y.id)">{{y.name}}</button></div><button class="filter-more mobile-more" :aria-expanded="!!expanded.years" @click="expanded.years=!expanded.years">{{expanded.years?'收起':'更多'}}</button></div>
  <form class="year-range" @submit.prevent="applyYears"><span>自定年份</span><input v-model="draftFrom" inputmode="numeric" maxlength="4" placeholder="起始年份" aria-label="起始年份"/><span>至</span><input v-model="draftTo" inputmode="numeric" maxlength="4" placeholder="结束年份" aria-label="结束年份"/><button type="submit">应用</button></form><p v-if="yearError" class="year-error" role="alert">{{yearError}}</p>
  <div class="browse-filter-row"><span class="filter-label">地区</span><div class="filter-options" :class="{expanded:expanded.countries}"><button class="filter-chip" :class="{active:!country}" @click="country=''">全部</button><button v-for="(c,index) in countries" :key="c.id" class="filter-chip" :class="{active:country===c.id,'extra-option':index>=16}" :hidden="index>=16&&!expanded.countries&&country!==c.id" @click="country=c.id">{{c.name}}</button></div><button class="filter-more" :aria-expanded="!!expanded.countries" @click="expanded.countries=!expanded.countries">{{expanded.countries?'收起':'更多'}}</button></div>
  <div class="browse-filter-row"><span class="filter-label">语言</span><div class="filter-options" :class="{expanded:expanded.languages}"><button class="filter-chip" :class="{active:!language}" @click="language=''">全部</button><button v-for="(l,index) in languages" :key="l.id" class="filter-chip" :class="{active:language===l.id}" :hidden="index>=13&&!expanded.languages&&language!==l.id" @click="language=l.id">{{l.name}}</button></div><button class="filter-more" :aria-expanded="!!expanded.languages" @click="expanded.languages=!expanded.languages">{{expanded.languages?'收起':'更多'}}</button></div>
  <div class="browse-selected"><div><span v-if="!selected.length">全部{{mediaType==='movie'?'电影':'剧集'}}</span><span v-for="s in selected" :key="s" class="selected-condition">{{s}}</span></div><button @click="reset"><RotateCcw :size="12"/>一键重置</button></div>
 </div>
 <p class="browse-help">地区为出品国家／地区，语言为影片原始语言，不代表资源配音。<span v-if="options.stale">筛选资料来自缓存，更新于 {{new Date(options.updated*1000).toLocaleString('zh-CN')}}。</span></p>
 <p v-if="notice" class="browse-notice" role="status">{{notice}}</p>
 <p class="discovery-caption">TMDB 影视库 · {{items.length}} 部已加载<span v-if="stale"> · 缓存资料，更新于 {{new Date(updated*1000).toLocaleString('zh-CN')}}</span></p>
 <div v-if="error" class="cinema-notice"><span>{{error}}</span><button @click="loadMore(!items.length)">重试</button></div>
 <PosterWall :items="items" @open="emit('open',$event)"/>
 <div v-if="!loading&&!error&&!items.length" class="browse-empty"><h2>没有符合条件的影视</h2><p>试着减少一些条件，发现更多好故事。</p><button class="cinema-button secondary" @click="reset">清空筛选</button></div>
 <div v-if="!loading&&items.length&&page<total" class="poster-more"><button class="cinema-button secondary" @click="loadMore()">加载更多海报 <ArrowRight :size="15"/></button></div>
 </template>
 <div v-if="loading" class="cinema-loading"><Loader2 class="spin" :size="24"/>正在加载{{options?'影视':'筛选选项'}}</div>
</div>
</template>
