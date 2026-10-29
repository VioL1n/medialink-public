<script setup lang="ts">
import {computed,ref} from 'vue'
import {ChevronDown,ChevronRight,Folder,Film,FileText,File} from 'lucide-vue-next'
import {fileBytes,roleLabels} from './magnetTree'
import type {MagnetNode} from './magnetTree'
const props=defineProps<{node:MagnetNode;chosen:Set<string>;disabled?:boolean}>()
const emit=defineEmits<{toggle:[ids:string[],checked:boolean]}>()
const expanded=ref(true)
const count=computed(()=>props.node.ids.filter(id=>props.chosen.has(id)).length)
const checked=computed(()=>count.value===props.node.ids.length)
const mixed=computed(()=>count.value>0&&!checked.value)
</script>
<template>
  <li>
    <div class="magnet-tree-row">
      <button v-if="!node.file" type="button" class="magnet-expand" :aria-label="(expanded?'收起目录 ':'展开目录 ')+node.path" :aria-expanded="expanded" @click="expanded=!expanded"><ChevronDown v-if="expanded" :size="14"/><ChevronRight v-else :size="14"/></button><span v-else class="magnet-expand-spacer"/>
      <label :title="node.path">
        <input type="checkbox" :checked="checked" :indeterminate="mixed" :aria-checked="mixed?'mixed':checked" :aria-label="(node.file?'选择文件 ':'选择目录 ')+node.path" :disabled="disabled" @change="emit('toggle',node.ids,($event.target as HTMLInputElement).checked)"/>
        <Folder v-if="!node.file" :size="15"/><Film v-else-if="node.file.kind==='video'" :size="15"/><FileText v-else-if="node.file.kind==='subtitle'" :size="15"/><File v-else :size="15"/>
        <span>{{node.name}}</span><em v-if="node.file?.role&&roleLabels[node.file.role]" class="magnet-role" :title="node.file.reason">{{roleLabels[node.file.role]}}</em>
      </label>
      <small>{{fileBytes(node.size)}}</small>
    </div>
    <ul v-if="!node.file&&expanded"><MagnetTree v-for="child in node.children" :key="child.path" :node="child" :chosen="chosen" :disabled="disabled" @toggle="(ids,checked)=>emit('toggle',ids,checked)"/></ul>
  </li>
</template>
