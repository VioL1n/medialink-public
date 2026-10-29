<script setup lang="ts">
import {Cloud,Flame,Magnet,Play} from 'lucide-vue-next'
import MediaImage from './MediaImage.vue'
import {ratingList,resourceChips} from './homeFeed'
import type {HomeItem} from './types'
defineProps<{items:HomeItem[]}>()
const emit=defineEmits<{open:[item:HomeItem]}>()
const icons={magnet:Magnet,cloud:Cloud,online:Play}
const marks={douban:'豆',imdb:'IMDb',mal:'MAL',tmdb:'TMDB'}
</script>
<template>
  <div class="poster-grid home-grid"><button v-for="item in items" :key="item.id" class="poster-card home-card" :title="item.title" @click="emit('open',item)">
    <div class="poster-cover"><MediaImage :src="item.poster||undefined" :alt="item.title"/>
      <span class="home-chips"><span v-for="chip in resourceChips(item)" :key="chip.kind" class="home-chip" :class="chip.kind" :title="chip.title"><template v-if="chip.kind!=='quality'">{{chip.label}}<component :is="icons[chip.kind]" :size="10" :stroke-width="2.4"/></template><template v-else>{{chip.label}}</template></span></span>
      <span v-if="ratingList(item).length||item.status" class="home-ratings"><span class="home-scores"><span v-for="rating in ratingList(item)" :key="rating.kind" :class="'score-'+rating.kind" :title="rating.title"><Flame v-if="rating.kind==='heat'" :size="11"/><b v-else>{{marks[rating.kind]}}</b>{{rating.label}}</span></span><span v-if="item.status" class="home-status">{{item.status}}</span></span>
    </div>
    <h3>{{item.title}}</h3><p class="home-tags">{{[item.year,...item.tags].filter(Boolean).join(' / ')}}</p>
  </button></div>
</template>
