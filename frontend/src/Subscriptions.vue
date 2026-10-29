<script setup lang="ts">
import {onBeforeUnmount,onMounted,ref} from 'vue'
import {AlertCircle,ArrowRight,Folder,Pause,Pencil,Play,RefreshCw,Rss,Trash2} from 'lucide-vue-next'
import MediaImage from './MediaImage.vue'
import {checkLabel,episodeStatus,ruleSummary,splitWords} from './anime'
import type {RequestFn,Subscription} from './types'
const props=defineProps<{request:RequestFn}>()
const emit=defineEmits<{task:[id:string];discover:[]}>()
const items=ref<Subscription[]>([]), loaded=ref(false), error=ref(''), busy=ref<string|null>(null), now=ref(Date.now())
const editing=ref<{id:string;target:string;resolution:string;include:string;exclude:string}|null>(null)
let timer:number|undefined

async function load(){try{items.value=await props.request<Subscription[]>('/subscriptions');now.value=Date.now()}catch(e:any){error.value=e.message}finally{loaded.value=true}}
async function act(subscription:Subscription,fn:()=>Promise<Subscription|null>){
  busy.value=subscription.id;error.value=''
  try{const next=await fn();items.value=next?items.value.map(s=>s.id===next.id?next:s):items.value.filter(s=>s.id!==subscription.id)}catch(e:any){error.value=e.message}finally{busy.value=null}
}
const check=(s:Subscription)=>act(s,()=>props.request(`/subscriptions/${s.id}/check`,'POST',{}))
const toggle=(s:Subscription)=>act(s,()=>props.request(`/subscriptions/${s.id}`,'PATCH',{enabled:!s.enabled}))
function remove(s:Subscription){if(confirm(`取消订阅《${s.title}》？已创建的入库任务和媒体库文件会保留。`))act(s,async()=>{await props.request(`/subscriptions/${s.id}`,'DELETE');return null})}
function edit(s:Subscription){editing.value={id:s.id,target:s.target,resolution:s.rules.resolution||'',include:s.rules.include.join(' '),exclude:s.rules.exclude.join(' ')}}
function save(s:Subscription){const draft=editing.value;if(!draft)return;act(s,async()=>{const next=await props.request<Subscription>(`/subscriptions/${s.id}`,'PATCH',{target:draft.target.trim(),rules:{resolution:draft.resolution||null,include:splitWords(draft.include),exclude:splitWords(draft.exclude)}});editing.value=null;return next})}
onMounted(()=>{load();timer=window.setInterval(load,15000)})
onBeforeUnmount(()=>clearInterval(timer))
</script>
<template><section>
  <div class="page-heading"><div><span class="eyebrow">NEW EPISODES, ON THEIR OWN</span><h1>追番订阅<span>.</span></h1><p>订阅的番剧每 30 分钟检查一次，新集自动下载、规范命名并通知 Emby。</p></div><button class="outline" @click="emit('discover')"><Rss :size="16"/>去每日番剧订阅</button></div>
  <div v-if="error" class="banner error"><AlertCircle :size="18"/><span>{{error}}</span></div>
  <div v-if="loaded&&!items.length" class="empty-state"><Rss :size="38"/><h3>还没有订阅</h3><p>在“发现 → 每日番剧”选择一部正在播出的番剧，订阅后新集会自动入库。</p><button class="text-button" @click="emit('discover')">去看看今天更新 <ArrowRight :size="15"/></button></div>
  <div class="subscription-list">
    <article v-for="s in items" :key="s.id" class="subscription-card" :class="{paused:!s.enabled}">
      <MediaImage class="subscription-poster" :src="s.poster||undefined" :alt="s.title"/>
      <div class="subscription-body">
        <div class="subscription-top"><h3>{{s.title}}</h3><span class="subscription-state"><span class="online-dot" :class="{paused:!s.enabled}"/>{{s.enabled?'追更中':'已暂停'}}</span></div>
        <p class="subscription-meta">{{s.subgroup_name||'全部字幕组'}} · {{ruleSummary(s.rules)}}<template v-if="s.media_type==='tv'"> · 第 {{s.season_number}} 季</template></p>
        <p class="task-path"><Folder :size="14"/>{{s.target}}</p>
        <p class="subscription-meta">上次检查：{{checkLabel(s.checked,now)}}<template v-if="s.enabled"> · 下次：{{checkLabel(s.next_check,now)}}</template></p>
        <p v-if="s.error" class="task-error">{{s.error}}</p>
        <div v-if="s.episodes.length" class="subscription-episodes"><button v-for="e in s.episodes" :key="e.episode" class="episode-chip" :class="episodeStatus(e).tone" :disabled="!e.task_id" :title="e.title" @click="e.task_id&&emit('task',e.task_id)">{{e.episode}}<small>{{episodeStatus(e).label}}</small></button></div>
        <p v-else class="subscription-meta">尚未发现符合规则的单集</p>
        <form v-if="editing?.id===s.id" class="subscription-edit" @submit.prevent="save(s)"><label>清晰度<select v-model="editing.resolution"><option value="1080P">1080P</option><option value="2160P">2160P</option><option value="720P">720P</option><option value="">不限</option></select></label><label>必须包含<input v-model="editing.include"/></label><label>排除<input v-model="editing.exclude"/></label><label>入库分类目录<input v-model="editing.target" required/></label><div><button type="button" class="outline" @click="editing=null">取消</button><button class="primary" :disabled="busy===s.id">保存</button></div></form>
        <div class="task-actions"><button class="text-button" :disabled="busy===s.id" @click="check(s)"><RefreshCw :size="13" :class="{spin:busy===s.id}"/>立即检查</button><div><button class="text-button" :disabled="busy===s.id" @click="edit(s)"><Pencil :size="13"/>编辑</button><button class="text-button" :disabled="busy===s.id" @click="toggle(s)"><Pause v-if="s.enabled" :size="13"/><Play v-else :size="13"/>{{s.enabled?'暂停':'继续'}}</button><button class="text-button muted" :disabled="busy===s.id" @click="remove(s)"><Trash2 :size="13"/>取消订阅</button></div></div>
      </div>
    </article>
  </div>
</section></template>
