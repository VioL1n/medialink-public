<script setup lang="ts">
import {ref,watch} from 'vue'
import {Film} from 'lucide-vue-next'
const props=defineProps<{src?:string;alt:string;eager?:boolean}>()
const failed=ref(false)
watch(()=>props.src,()=>failed.value=false)
</script>
<template><div class="media-image"><img v-if="src&&!failed" :src="src" :alt="alt" :loading="eager?'eager':'lazy'" decoding="async" @error="failed=true"/><div v-else class="poster-placeholder"><Film :size="32"/><span>{{alt}}</span></div></div></template>
