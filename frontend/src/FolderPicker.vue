<script setup lang="ts">
import {computed,nextTick,onMounted,ref,watch} from 'vue'
import {Check,ChevronLeft,ChevronRight,Folder,FolderPlus,Loader2,Pencil} from 'lucide-vue-next'
import type {RequestFn} from './types'
type Directory={name:string;path:string}
const props=defineProps<{request:RequestFn;modelValue:string;disabled?:boolean}>()
const emit=defineEmits<{'update:modelValue':[path:string];editing:[active:boolean];renamed:[lastTarget:string]}>()
const root=ref(''), directoryPath=ref(''), directories=ref<Directory[]>([]), busy=ref(false), error=ref(''), status=ref('')
const folderEdit=ref<{mode:'create'|'rename';path:string;parent:string}|null>(null), folderName=ref(''), folderFilter=ref(''), folderList=ref<HTMLElement|null>(null)
const locked=computed(()=>busy.value||!!props.disabled||!!folderEdit.value)
const atRoot=computed(()=>directoryPath.value===root.value)
const parentPath=(path:string)=>path.split('/').slice(0,-1).join('/')||'/'
// Breadcrumbs start at the media root, which is the highest directory the server will list.
const folderCrumbs=computed(()=>{const base=root.value.replace(/\/$/,'')||'/';const rest=directoryPath.value.startsWith(base)?directoryPath.value.slice(base.length).split('/').filter(Boolean):[];const crumbs=[{name:base.split('/').filter(Boolean).pop()||'媒体库',path:base}];for(const name of rest)crumbs.push({name,path:crumbs[crumbs.length-1].path.replace(/\/$/,'')+'/'+name});return crumbs})
const visibleDirectories=computed(()=>{const q=folderFilter.value.trim().toLowerCase();return q?directories.value.filter(d=>d.name.toLowerCase().includes(q)):directories.value})
watch(folderEdit,edit=>emit('editing',!!edit))
const select=(path:string)=>emit('update:modelValue',path)

async function run(fn:()=>Promise<void>){busy.value=true;error.value='';try{await fn()}catch(e:any){error.value=e.message}finally{busy.value=false}}
async function browse(path:string,refresh=false){const data=await props.request<{path:string;root?:string;directories:Directory[]}>('/directories?path='+encodeURIComponent(path)+'&refresh='+refresh);if(data.path!==directoryPath.value)folderFilter.value='';root.value=data.root||root.value||await mediaRoot();directoryPath.value=data.path;directories.value=data.directories;await nextTick();revealTarget()}
// Servers started before `root` was added resolve an empty path to the media root, so ask for that.
async function mediaRoot(){return (await props.request<{path:string}>('/directories?path=')).path}
// Keep the chosen folder in view inside the list without scrolling the whole dialog.
function revealTarget(){const list=folderList.value,row=list?.querySelector<HTMLElement>('.folder-row.selected');if(list&&row)list.scrollTop=row.offsetTop-list.clientHeight/2+row.offsetHeight/2}
function editFolder(directory?:Directory){error.value='';status.value='';folderName.value=directory?.name||'';folderEdit.value={mode:directory?'rename':'create',path:directory?.path||'',parent:directory?parentPath(directory.path):directoryPath.value}}
function replaceFolderPath(path:string,oldPath:string,newPath:string){return path===oldPath||path.startsWith(oldPath+'/')?newPath+path.slice(oldPath.length):path}
async function saveFolder(){
  if(!folderEdit.value||!folderName.value.trim())return
  const edit=folderEdit.value
  await run(async()=>{
    try{
      const result=await props.request('/directories',edit.mode==='create'?'POST':'PATCH',{parent:edit.parent,path:edit.path,name:folderName.value.trim()})
      if(edit.mode==='create')select(result.path)
      else{select(replaceFolderPath(props.modelValue,edit.path,result.path));emit('renamed',result.last_target)}
      folderEdit.value=null
      await browse(edit.mode==='create'?edit.parent:replaceFolderPath(directoryPath.value,edit.path,result.path),true)
      status.value=edit.mode==='create'?'文件夹已新建并选为保存位置':'文件夹已重命名'
    }catch(e){try{await browse(edit.parent,true)}catch{}throw e}
  })
}
// Open beside the current target so it shows selected among its siblings, one click from any neighbour.
onMounted(()=>run(async()=>{const path=props.modelValue;const start=path&&parentPath(path);try{await browse(start||path)}catch{await browse(path)}}))
</script>

<template>
  <div class="folder-picker">
    <div class="folder-tools"><span>点选文件夹即设为保存位置，点右侧箭头进入下一级</span><button type="button" class="outline" :disabled="locked||!directoryPath" @click="editFolder()"><FolderPlus :size="15"/>新建文件夹</button></div>
    <div v-if="folderEdit" class="folder-editor"><label>{{folderEdit.mode==='create'?'新文件夹名称':'文件夹新名称'}}<input v-model="folderName" :aria-label="folderEdit.mode==='create'?'新文件夹名称':'文件夹新名称'" autocomplete="off" :disabled="busy" @keydown.enter.prevent="saveFolder"/></label><div><button type="button" class="outline" :disabled="busy" @click="folderEdit=null">取消</button><button type="button" class="primary" :disabled="busy||!folderName.trim()" @click="saveFolder"><Loader2 v-if="busy" class="spin" :size="15"/>{{folderEdit.mode==='create'?'创建并选择':'保存名称'}}</button></div></div>
    <div ref="folderList" class="folder-browser" :aria-busy="busy"><div class="folder-header"><button type="button" class="icon-button" aria-label="返回上级目录" :disabled="locked||atRoot" @click="run(()=>browse(parentPath(directoryPath)))"><ChevronLeft :size="16"/></button><nav class="folder-crumbs" aria-label="当前位置" :title="directoryPath"><template v-for="(c,i) in folderCrumbs" :key="c.path"><ChevronRight v-if="i" :size="12"/><button v-if="i<folderCrumbs.length-1" type="button" :disabled="locked" @click="run(()=>browse(c.path))">{{c.name}}</button><b v-else>{{c.name}}</b></template></nav><Loader2 v-if="busy" class="spin" :size="14"/><button type="button" class="icon-button" aria-label="重命名当前文件夹" :disabled="locked||atRoot" @click="editFolder({name:directoryPath.split('/').pop()||'',path:directoryPath})"><Pencil :size="14"/></button><button type="button" class="folder-choose-here" :class="{chosen:modelValue===directoryPath}" :disabled="locked||!directoryPath" @click="select(directoryPath)"><Check v-if="modelValue===directoryPath" :size="13"/>{{modelValue===directoryPath?'已选此目录':'选择此目录'}}</button></div><div v-if="directories.length>8" class="folder-filter"><input v-model="folderFilter" placeholder="筛选文件夹" aria-label="筛选文件夹" autocomplete="off" @keydown.enter.prevent/></div><div v-for="d in visibleDirectories" :key="d.path" class="folder-row" :class="{selected:modelValue===d.path}"><button type="button" class="folder-pick" :disabled="locked" :aria-pressed="modelValue===d.path" :title="'选择 '+d.path+'（双击进入）'" @click="select(d.path)" @dblclick="run(()=>browse(d.path))"><Folder :size="17"/><span>{{d.name}}</span><Check v-if="modelValue===d.path" :size="15"/></button><button type="button" class="folder-rename icon-button" :disabled="locked" :aria-label="'重命名文件夹 '+d.name" @click="editFolder(d)"><Pencil :size="14"/></button><button type="button" class="folder-enter" :disabled="locked" :aria-label="'进入文件夹 '+d.name" @click="run(()=>browse(d.path))"><ChevronRight :size="17"/></button></div><p v-if="directoryPath&&!directories.length">此目录没有子文件夹，可直接选择此目录或新建文件夹</p><p v-else-if="directories.length&&!visibleDirectories.length">没有匹配的文件夹</p></div>
    <p v-if="error" class="error-text" role="alert">{{error}}</p><p v-else-if="status" class="folder-status" role="status">{{status}}</p>
  </div>
</template>
