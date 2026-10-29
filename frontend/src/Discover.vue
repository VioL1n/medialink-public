<script setup lang="ts">
import {computed,ref,watch} from 'vue'
import {ArrowRight,Film} from 'lucide-vue-next'
import HomeFeed from './HomeFeed.vue'
import CategoryBrowse from './CategoryBrowse.vue'
import AnimeSchedule from './AnimeSchedule.vue'
import type {Media,RequestFn} from './types'
const props=defineProps<{request:RequestFn;configured:boolean;animeTab?:number}>()
const emit=defineEmits<{open:[media:Pick<Media,'type'|'id'>];search:[title:string];settings:[];subscriptions:[]}>()
const mode=ref<'home'|'anime'|'browse'>('home')
watch(()=>props.animeTab,()=>mode.value='anime')
const heading=computed(()=>mode.value==='anime'?{eyebrow:'ON AIR THIS WEEK',title:'每日番剧',text:'蜜柑计划当季番组按星期排列，订阅后新集自动入库。'}:{eyebrow:'FRESHLY ARRIVED, READY TO COLLECT',title:'最近更新',text:'只看已经有磁力或网盘资源的新片，点开即可入库。'})
</script>
<template><section class="cinema-page discover-page">
  <div class="discovery-top"><div><span class="eyebrow">{{heading.eyebrow}}</span><h1>{{heading.title}}<span>.</span></h1><p>{{heading.text}}</p></div></div>
  <div class="discovery-mode cinema-tabs" aria-label="发现浏览方式"><button :class="{active:mode==='home'}" @click="mode='home'">首页</button><button :class="{active:mode==='anime'}" @click="mode='anime'">每日番剧</button><button :class="{active:mode==='browse'}" @click="mode='browse'">分类浏览</button></div>
  <HomeFeed v-show="mode==='home'" :request="request" @open="emit('open',$event)" @search="emit('search',$event)" @settings="emit('settings')"/>
  <AnimeSchedule v-show="mode==='anime'" :request="request" :enabled="mode==='anime'" @open="emit('open',$event)" @search="emit('search',$event)" @subscriptions="emit('subscriptions')"/>
  <div v-show="mode==='browse'">
    <div v-if="!configured" class="cinema-setup"><Film :size="38"/><span class="eyebrow">LIGHTS. CAMERA. DISCOVER.</span><h2>让下一部好片，自己出现。</h2><p>连接 TMDB，按类型、年份和地区浏览影视，<br/>再用中英文片名找到资源。</p><button class="cinema-button" @click="emit('settings')">配置 TMDB <ArrowRight :size="17"/></button><small>在设置中填写 API Key 或 Read Access Token</small></div>
    <CategoryBrowse v-else :enabled="configured&&mode==='browse'" :request="request" @open="emit('open',$event)" @settings="emit('settings')"/>
  </div>
</section></template>
