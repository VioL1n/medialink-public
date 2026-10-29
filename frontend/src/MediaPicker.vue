<script setup lang="ts">
import {ref} from 'vue'
import type {Media,MediaSelection,RequestFn} from './types'
const props=defineProps<{request:RequestFn;modelValue?:MediaSelection|null;title?:string;disabled?:boolean}>()
const emit=defineEmits<{'update:modelValue':[media:MediaSelection|null];'update:title':[title:string]}>()
const query=ref(''), results=ref<Media[]>([]), loading=ref(false), error=ref('')
let generation=0
async function search(){const token=++generation;loading.value=true;error.value='';results.value=[];try{const data=await props.request('/tmdb/search?query='+encodeURIComponent(query.value.trim()));if(token===generation)results.value=data.items}catch(e:any){if(token===generation)error.value=e.message}finally{if(token===generation)loading.value=false}}
function choose(media:Media){generation++;loading.value=false;emit('update:modelValue',{type:media.type,id:media.id});emit('update:title',media.title);results.value=[]}
function season(value:string){if(props.modelValue)emit('update:modelValue',{...props.modelValue,season_number:value===''?undefined:Number(value)})}
</script>
<template>
  <section class="media-picker">
    <b>关联 TMDB 影片</b>
    <p v-if="modelValue">{{title||'TMDB '+modelValue.id}} · {{modelValue.type==='movie'?'电影':'剧集'}} <button type="button" class="text-button" :disabled="disabled" @click="emit('update:modelValue',null);emit('update:title','')">清除关联</button></p>
    <p v-else class="modal-note">未关联时会保留暂存文件，等待补全后继续。每个任务仅对应一部电影或剧集，合集请拆分入库。</p>
    <div v-if="!disabled" class="media-picker-search"><input v-model="query" aria-label="TMDB 影片关键词" placeholder="搜索电影或剧集" @keydown.enter.prevent="query.trim()&&search()"/><button type="button" class="outline" :disabled="loading||!query.trim()" @click="search">{{loading?'搜索中':'搜索 TMDB'}}</button></div>
    <p v-if="error" class="error-text" role="alert">{{error}}</p>
    <div v-if="results.length" class="media-picker-results"><button v-for="media in results" :key="media.type+media.id" type="button" class="outline" @click="choose(media)">{{media.title}} · {{media.year||'年份未知'}} · {{media.type==='movie'?'电影':'剧集'}}</button></div>
    <label v-if="modelValue?.type==='tv'">所选季数（可选，0 为特别篇）<input type="number" min="0" max="1000" aria-label="所选季数" :disabled="disabled" :value="modelValue.season_number??''" @input="season(($event.target as HTMLInputElement).value)"/></label>
  </section>
</template>
