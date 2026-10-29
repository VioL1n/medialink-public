<script setup lang="ts">
import {ArrowUpRight,Star} from 'lucide-vue-next'
import MediaImage from './MediaImage.vue'
import type {Media} from './types'
defineProps<{items:Media[]}>()
const emit=defineEmits<{open:[media:Media]}>()
</script>
<template>
    <div class="poster-grid"><button v-for="item in items" :key="item.type+item.id" class="poster-card" @click="emit('open',item)"><div class="poster-cover"><MediaImage :src="item.poster" :alt="item.title"/><span class="poster-type">{{item.type==='movie'?'电影':'剧集'}}</span><span v-if="item.rating" class="poster-rating"><Star :size="11"/>{{item.rating.toFixed(1)}}</span><span class="poster-open"><ArrowUpRight :size="22"/></span></div><h3>{{item.title}}</h3><p>{{item.year||'年份未知'}}<span>{{item.type==='movie'?'电影':'剧集'}}</span></p></button></div>
</template>
