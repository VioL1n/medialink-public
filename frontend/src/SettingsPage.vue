<script setup lang="ts">
import {computed, nextTick, onBeforeUnmount, reactive, ref, watch} from 'vue'
import {Search, Film, Radio, Sparkles, Download, Folder, Globe, LockKeyhole, Check, RefreshCw, Loader2, CheckCircle2, AlertCircle, ArrowUpRight} from 'lucide-vue-next'
import {copySettings, mergeSettingsResponse, reconcileDraft, settingsPatch} from './settingsState'
import type {SettingsValues} from './settingsState'
import type {RequestFn, SearchSource} from './types'

const props = defineProps<{request:RequestFn; values:SettingsValues; sources:SearchSource[]}>()
const emit = defineEmits<{saved:[values:SettingsValues]; channels:[]; passwordChanged:[]}>()
const tabs = [
  {id:'search', name:'搜索偏好', icon:Search},
  {id:'connections', name:'服务连接', icon:Radio},
  {id:'network', name:'目录与网络', icon:Folder},
  {id:'security', name:'安全与访问', icon:LockKeyhole},
]
const tab = ref('search')
const saved = ref(copySettings(props.values)), draft = ref(copySettings(props.values))
const states = reactive<Record<string, {busy:boolean; error:string; message:string}>>({})
const searchFields = ['enabled_search_sources', 'default_use_tmdb', 'knaben_url', 'limetorrents_url', 'nyaa_url', 'mikan_url', 'yts_url', 'eztv_url']
const connectionFields = ['openlist_url', 'openlist_db', 'emby_url']
const llmFields = ['llm_enabled', 'llm_base_url', 'llm_model', 'llm_use_proxy']
const networkFields = ['media_root', 'strm_root', 'staging_root', 'strm_local', 'anime_target', 'proxy', 'magnet_use_proxy']
const accessFields = ['public_access_enabled', 'public_access_hosts']
const sites = [
  {key:'knaben_url', name:'Knaben API 地址'}, {key:'limetorrents_url', name:'LimeTorrents 地址'},
  {key:'nyaa_url', name:'Nyaa 地址'},
  {key:'mikan_url', name:'蜜柑计划地址（每日番剧与追番订阅）'},
  {key:'yts_url', name:'YTS 地址（首页最新电影）'}, {key:'eztv_url', name:'EZTV 地址（首页最新剧集）'},
]
const directories = [
  {key:'media_root', name:'媒体根目录'}, {key:'strm_root', name:'STRM 挂载目录'},
  {key:'staging_root', name:'任务暂存目录'}, {key:'strm_local', name:'本地 STRM 输出目录'},
  {key:'anime_target', name:'番剧默认入库目录（可选，留空用上次目录）'},
]
const health = ref<Record<string, {ok:boolean; message:string}>>({})
const healthNames:Record<string,string> = {openlist:'OpenList', emby:'Emby', p115:'115 网盘', telegram:'Telegram API', telegram_web:'Telegram 公开网页', tmdb:'TMDB'}
const tmdbCredential = ref(''), tmdbType = ref<'token'|'api_key'>(saved.value.tmdb_auth_type || 'token')
const tmdbHealth = ref<{ok:boolean; message:string}|null>(null)
const llmKey = ref(''), llmHealth = ref<{ok:boolean; message:string}|null>(null)
const apiHash = ref(''), openlistToken = ref(''), embyToken = ref(''), cookies = ref('')
const phone = ref(''), tgCode = ref(''), tgPassword = ref(''), tgStage = ref('phone')
const qr = ref(false), qrNonce = ref(0), qrStatus = ref('waiting')
const currentPassword = ref(''), newPassword = ref(''), confirmPassword = ref('')
let qrTimer:ReturnType<typeof setTimeout>|undefined, alive = true
function state(key:string) { return states[key] ||= {busy:false, error:'', message:''} }
function dirty(fields:string[]) { return fields.some(key => JSON.stringify(draft.value[key]) !== JSON.stringify(saved.value[key])) }
const searchDirty = computed(() => dirty(searchFields))
const networkDirty = computed(() => dirty(networkFields))
const accessDirty = computed(() => dirty(accessFields))
const connectionsDirty = computed(() => dirty(connectionFields) || !!openlistToken.value || !!embyToken.value)
const telegramDirty = computed(() => dirty(['telegram_api_id']) || !!apiHash.value)
const llmDirty = computed(() => dirty(llmFields) || !!llmKey.value.trim())
function accept(next:SettingsValues, fields:string[] = []) {
  if (!alive) return
  draft.value = reconcileDraft(saved.value, draft.value, next, fields)
  saved.value = copySettings(next)
  emit('saved', next)
}
watch(() => props.values, next => {
  draft.value = reconcileDraft(saved.value, draft.value, next)
  saved.value = copySettings(next)
})
async function perform(key:string, fn:()=>Promise<void>) {
  const status = state(key)
  if (status.busy) return
  status.busy = true; status.error = ''; status.message = ''
  try { await fn() } catch(e:any) { if(alive) status.error = e.message } finally { status.busy = false }
}
async function patch(values:SettingsValues) {
  const next = await props.request('/settings', 'PATCH', values)
  accept(mergeSettingsResponse(saved.value, next, Object.keys(values)), Object.keys(values))
}
async function refreshConnections() {
  const next = await props.request('/settings')
  accept(mergeSettingsResponse(saved.value, next, ['telegram_connected', 'telegram_hash_saved', 'p115_connected', 'tmdb_configured', 'tmdb_auth_type']))
}
async function saveGroup(key:string, fields:string[]) {
  await perform(key, async () => {
    const values = settingsPatch(draft.value, fields)
    if (key === 'network' && values.media_root !== saved.value.media_root) {
      const last = saved.value.last_target as string
      if (last !== values.media_root && !last.startsWith(values.media_root.replace(/\/$/, '') + '/')) values.last_target = values.media_root
    }
    await patch(values)
    state(key).message = key === 'access' ? '访问设置已保存，立即生效；关闭公网访问后请使用本机或局域网地址。' : '设置已保存'
  })
}
async function saveConnections() {
  await perform('connections', async () => {
    const values = settingsPatch(draft.value, connectionFields)
    if (openlistToken.value) values.openlist_token = openlistToken.value
    if (embyToken.value) values.emby_token = embyToken.value
    await patch(values); openlistToken.value = ''; embyToken.value = ''; state('connections').message = '连接配置已保存'
  })
}
async function saveTelegram() {
  await perform('telegram', async () => {
    const values = settingsPatch(draft.value, ['telegram_api_id'])
    if (apiHash.value) values.telegram_api_hash = apiHash.value
    await patch(values); apiHash.value = ''; state('telegram').message = 'API 配置已保存'
  })
}
async function saveTmdb(clear = false) {
  await perform('tmdb', async () => {
    await patch({tmdb_credential:clear ? null : {type:tmdbType.value, value:tmdbCredential.value}})
    tmdbCredential.value = ''; tmdbHealth.value = null; state('tmdb').message = clear ? 'TMDB 凭据已清除' : 'TMDB 凭据已保存到凭据存储'
  })
}
async function check() { await perform('check', async () => { health.value = await props.request('/settings/check') }) }
async function checkTmdb() {
  await perform('tmdb', async () => { tmdbHealth.value = await props.request('/tmdb/check') })
}
async function saveLlm(clear = false) {
  await perform('llm', async () => {
    const values = clear ? {llm_api_key:null, llm_enabled:false} : settingsPatch(draft.value, llmFields)
    if (!clear && llmKey.value.trim()) values.llm_api_key = llmKey.value.trim()
    await patch(values)
    llmKey.value = ''; llmHealth.value = null
    state('llm').message = clear ? '大模型密钥已清除，辅助命名已关闭' : '大模型配置已保存'
  })
}
async function checkLlm() {
  await perform('llm', async () => { llmHealth.value = await props.request('/llm/check', 'POST', {}) })
}
async function sendCode() {
  await perform('telegram', async () => { await props.request('/telegram/code', 'POST', {phone:phone.value}); tgStage.value = 'code'; state('telegram').message = '请查看 Telegram 登录验证码' })
}
async function tgLogin() {
  await perform('telegram', async () => {
    const result = await props.request('/telegram/login', 'POST', tgStage.value === 'password' ? {password:tgPassword.value} : {code:tgCode.value})
    tgCode.value = ''; tgPassword.value = ''
    if (result.status === 'password_required') tgStage.value = 'password'
    else { tgStage.value = 'phone'; await refreshConnections(); state('telegram').message = 'Telegram 已登录' }
  })
}
function stopQR() { if(qrTimer) clearTimeout(qrTimer); qrTimer = undefined }
async function pollQR(nonce = qrNonce.value) {
  try {
    const result = await props.request('/p115/qr/poll', 'POST', {})
    if (!alive || nonce !== qrNonce.value) return
    qrStatus.value = result.status
    if (result.status === 'connected') { qr.value = false; await refreshConnections(); state('p115').message = '115 已登录，账号核对成功'; return }
    if (!['expired','canceled'].includes(result.status)) qrTimer = setTimeout(() => pollQR(nonce), 3000)
  } catch(e:any) { if(alive && nonce === qrNonce.value) { qrStatus.value = 'error'; state('p115').error = e.message } }
}
async function startQR() {
  stopQR(); qrNonce.value = Math.max(Date.now(), qrNonce.value + 1)
  const nonce = qrNonce.value
  await perform('p115', async () => {
    await props.request('/p115/qr', 'POST', {})
    if (!alive || nonce !== qrNonce.value) return
    qr.value = true; qrStatus.value = 'waiting'; qrTimer = setTimeout(() => pollQR(nonce), 1000)
  })
}
function resumeQR() { stopQR(); state('p115').error = ''; qrStatus.value = 'waiting'; qrTimer = setTimeout(() => pollQR(), 0) }
async function cookieLogin() {
  stopQR(); qrNonce.value++; qr.value = false
  await perform('p115', async () => { await props.request('/p115/login', 'POST', {cookies:cookies.value}); cookies.value = ''; await refreshConnections(); state('p115').message = '115 已连接' })
}
async function disconnect(which:'telegram'|'p115') {
  if(which === 'p115') { stopQR(); qrNonce.value++; qr.value = false }
  await perform(which, async () => { await props.request('/'+which+'/logout', 'POST', {}); await refreshConnections(); state(which).message = '已退出登录' })
}
async function changePassword() {
  await perform('password', async () => {
    if (newPassword.value !== confirmPassword.value) throw new Error('两次输入的新密码不一致')
    if (newPassword.value === currentPassword.value) throw new Error('新密码不能与当前密码相同')
    await props.request('/auth/password', 'POST', {current_password:currentPassword.value, new_password:newPassword.value})
    currentPassword.value = ''; newPassword.value = ''; confirmPassword.value = ''; emit('passwordChanged')
  })
}
async function moveTab(event:KeyboardEvent, index:number) {
  let next = index
  if(event.key === 'ArrowRight') next = (index+1)%tabs.length
  else if(event.key === 'ArrowLeft') next = (index+tabs.length-1)%tabs.length
  else if(event.key === 'Home') next = 0
  else if(event.key === 'End') next = tabs.length-1
  else return
  event.preventDefault(); tab.value = tabs[next]!.id; await nextTick(); document.getElementById('settings-tab-'+tab.value)?.focus()
}
onBeforeUnmount(() => { alive = false; stopQR(); qrNonce.value++ })
</script>

<template>
  <section class="settings-page">
    <div class="page-heading"><div><span class="eyebrow">MAKE IT YOURS</span><h1>设置<span>.</span></h1><p>选择搜索来源，连接媒体库，管理你的访问方式。</p></div></div>
    <div class="settings-tabs" role="tablist" aria-label="设置分类">
      <button v-for="(item,index) in tabs" :id="'settings-tab-'+item.id" :key="item.id" role="tab" :aria-selected="tab===item.id" :aria-controls="'settings-panel-'+item.id" :tabindex="tab===item.id?0:-1" :class="{active:tab===item.id}" @click="tab=item.id" @keydown="moveTab($event,index)"><component :is="item.icon" :size="17"/>{{item.name}}</button>
    </div>

    <div v-show="tab==='search'" id="settings-panel-search" role="tabpanel" aria-labelledby="settings-tab-search">
      <form class="settings-card" @submit.prevent="saveGroup('search',searchFields)"><fieldset :disabled="state('search').busy">
        <h2><Search :size="20"/>默认搜索源</h2><p>启用的来源会显示在资源搜索和影片详情中，并默认勾选。搜索时可临时取消。</p>
        <div class="settings-source-list"><label v-for="source in sources" :key="source.id" class="settings-source-option"><input v-model="draft.enabled_search_sources" type="checkbox" :value="source.id" :aria-label="'启用 '+source.name"/><span><b>{{source.name}}</b><small>{{source.kind==='index'?'搜索已索引的 Telegram 频道':'搜索网站资源'}}</small></span></label></div>
        <p v-if="!draft.enabled_search_sources.length" class="settings-hint">全部关闭后仍可浏览影视资料和手动入库，资源搜索暂时停用。</p>
        <div class="card-divider"/><label class="settings-checkbox"><input v-model="draft.default_use_tmdb" type="checkbox"/><span>普通搜索默认开启 TMDB 片名辅助</span></label><p>普通搜索可临时调整；影片详情始终根据已选影片搜索。TMDB 凭据在“服务连接”中配置。</p>
        <div class="card-divider"/><p>首页由 Telegram 频道、YTS、EZTV 与蜜柑计划聚合，需要配置 TMDB 凭据；国产剧集来自已添加的 Telegram 115 频道。</p>
        <details class="settings-advanced"><summary>网站地址配置</summary><label v-for="site in sites" :key="site.key">{{site.name}}<input v-model="draft[site.key]" :aria-label="site.name" required/></label><p>网站请求使用已保存的应用代理。</p></details>
        <p v-if="state('search').error" class="error-text" role="alert">{{state('search').error}}</p><p v-if="state('search').message" class="settings-success" role="status">{{state('search').message}}</p>
        <div class="settings-actions"><span>{{searchDirty?'有未保存的更改':'已与保存配置同步'}}</span><button class="primary" :disabled="!searchDirty"><Loader2 v-if="state('search').busy" :size="17" class="spin"/><Check v-else :size="17"/>保存搜索偏好</button></div>
      </fieldset></form>
    </div>

    <div v-show="tab==='connections'" id="settings-panel-connections" role="tabpanel" aria-labelledby="settings-tab-connections">
      <div class="settings-check-row"><p>登录与检测使用已保存配置。修改配置后，请先保存。</p><button class="outline" :disabled="state('check').busy" @click="check"><RefreshCw :size="16" :class="{spin:state('check').busy}"/>检测连接</button></div>
      <p v-if="state('check').error" class="error-text" role="alert">{{state('check').error}}</p>
      <div v-if="Object.keys(health).length" class="health-grid"><div v-for="(item,key) in health" :key="key"><CheckCircle2 v-if="item.ok" :size="18"/><AlertCircle v-else :size="18"/><b>{{healthNames[key]||key}}</b><small>{{item.message}}</small></div></div>
      <div class="settings-grid">
        <form class="settings-card llm-settings" @submit.prevent="saveLlm()"><fieldset :disabled="state('llm').busy">
          <h2><Sparkles :size="20"/>大模型辅助命名</h2>
          <p>规则无法解析清晰度或季集号时自动补全。生成建议后，在任务详情核对并确认重命名。</p>
          <label class="settings-checkbox"><input v-model="draft.llm_enabled" type="checkbox"/><span>启用大模型辅助命名</span></label>
          <label>大模型 Base URL<input v-model="draft.llm_base_url" type="url" placeholder="https://服务地址/compatible-mode/v1"/></label>
          <p class="settings-hint">使用 OpenAI 兼容地址，保留 /v1 等路径前缀，勿填写 /chat/completions。</p>
          <label>大模型名称<input v-model="draft.llm_model" placeholder="填写服务控制台中的模型 ID" autocomplete="off"/></label>
          <div v-if="saved.llm_key_saved" class="connected-row"><CheckCircle2 :size="18"/>API Key 已保存在凭据存储<button type="button" class="text-button" @click="saveLlm(true)">清除大模型密钥</button></div>
          <label>大模型 API Key<input v-model="llmKey" type="password" autocomplete="off" :placeholder="saved.llm_key_saved?'留空保留已保存密钥':'填写 API Key'"/></label>
          <label class="settings-checkbox"><input v-model="draft.llm_use_proxy" type="checkbox"/><span>大模型使用应用代理</span></label>
          <p class="settings-hint">默认直连；开启后使用“目录与网络”中已保存的代理。</p>
          <div class="tmdb-setting-actions"><button class="primary" :disabled="!llmDirty">保存大模型配置</button><button type="button" class="outline" :disabled="!saved.llm_configured" @click="checkLlm">检测大模型</button></div>
          <p class="settings-hint">检测使用已保存配置，发送一个小型命名样例。</p>
          <p v-if="llmHealth" :class="{'error-text':!llmHealth.ok}" role="status">{{llmHealth.message}}</p>
          <p v-if="state('llm').error" class="error-text" role="alert">{{state('llm').error}}</p><p v-if="state('llm').message" class="settings-success" role="status">{{state('llm').message}}</p>
        </fieldset></form>
        <form class="settings-card" @submit.prevent="saveTmdb()"><fieldset :disabled="state('tmdb').busy">
          <h2><Film :size="20"/>TMDB 影视资料</h2><p>热门海报、影片详情与片名辅助，共用应用代理。</p>
          <div v-if="saved.tmdb_configured" class="connected-row"><CheckCircle2 :size="18"/>已保存 {{saved.tmdb_auth_type==='api_key'?'API Key':'Read Access Token'}}<button type="button" class="text-button" @click="saveTmdb(true)">清除凭据</button></div>
          <label>授权类型<select v-model="tmdbType" aria-label="TMDB 授权类型"><option value="token">Read Access Token（推荐）</option><option value="api_key">API Key</option></select></label>
          <label>TMDB 凭据<input v-model="tmdbCredential" type="password" autocomplete="off" :placeholder="saved.tmdb_configured?'留空保留已保存凭据':'填写 API Read Access Token 或 API Key'"/></label>
          <div class="tmdb-setting-actions"><button class="primary" :disabled="!tmdbCredential.trim()">保存 TMDB</button><button type="button" class="outline" :disabled="!saved.tmdb_configured" @click="checkTmdb">检测 TMDB</button></div>
          <p v-if="tmdbHealth" :class="{'error-text':!tmdbHealth.ok}">{{tmdbHealth.message}}</p><p v-if="state('tmdb').error" class="error-text" role="alert">{{state('tmdb').error}}</p><p v-if="state('tmdb').message" class="settings-success" role="status">{{state('tmdb').message}}</p>
          <a href="https://www.themoviedb.org/settings/api" target="_blank" rel="noopener noreferrer" class="text-button">前往 TMDB 获取凭据 <ArrowUpRight :size="13"/></a>
        </fieldset></form>
        <div class="settings-card"><fieldset :disabled="state('telegram').busy">
          <h2><Radio :size="20"/>Telegram</h2><p>公开网页模式无需个人 API 或账号登录，每 5 分钟检查更新。</p><button class="outline wide" @click="emit('channels')">添加公开频道</button>
          <details class="telegram-api" :open="saved.telegram_connected"><summary>个人账号 API 模式（可选）</summary><p>私有频道或完整账号同步使用自己的 API ID 和 API Hash。</p>
            <label>API ID<input v-model="draft.telegram_api_id" inputmode="numeric" placeholder="从 my.telegram.org 获取"/></label><label>API Hash<input v-model="apiHash" type="password" autocomplete="off" :placeholder="saved.telegram_hash_saved?'已保存在凭据存储；留空保持':'填写 API Hash'"/></label>
            <button class="outline wide" :disabled="!telegramDirty" @click="saveTelegram">保存 API 配置</button><div class="card-divider"/>
            <div v-if="saved.telegram_connected" class="connected-row"><CheckCircle2 :size="18"/>已保存登录会话<button class="text-button" @click="disconnect('telegram')">退出</button></div>
            <template v-else><label v-if="tgStage==='phone'">手机号<input v-model="phone" placeholder="+86…" autocomplete="tel"/></label><button v-if="tgStage==='phone'" class="primary wide" :disabled="!phone||telegramDirty" @click="sendCode">获取登录验证码</button>
              <template v-else><label v-if="tgStage==='code'">Telegram 验证码<input v-model="tgCode" autocomplete="one-time-code"/></label><label v-else>两步验证密码<input v-model="tgPassword" type="password" autocomplete="off"/></label><button class="primary wide" :disabled="telegramDirty" @click="tgLogin">完成登录</button><button class="text-button" @click="tgStage='phone';tgCode='';tgPassword=''">重新发送验证码</button></template>
            </template><p v-if="telegramDirty" class="settings-hint">请先保存 API 配置，再登录。</p>
          </details><p v-if="state('telegram').error" class="error-text" role="alert">{{state('telegram').error}}</p><p v-if="state('telegram').message" class="settings-success" role="status">{{state('telegram').message}}</p>
        </fieldset></div>
        <div class="settings-card"><fieldset :disabled="state('p115').busy">
          <h2><Download :size="20"/>115 网盘</h2><p>磁力下载使用 OpenList 已有授权；分享转存需要单独登录，并核对两边账号。</p>
          <div v-if="saved.p115_connected" class="connected-row"><CheckCircle2 :size="18"/>已保存登录信息<button class="text-button" @click="disconnect('p115')">退出</button></div>
          <template v-else><button class="primary wide" @click="startQR">扫码登录 115</button>
            <div v-if="qr" class="qr-box"><img :src="'/api/v1/p115/qr/image?v='+qrNonce" alt="115 登录二维码"/><p>{{qrStatus==='scanned'?'已扫码，请在手机确认':qrStatus==='expired'?'二维码已过期，请重新获取':qrStatus==='canceled'?'手机已取消登录，请重新获取':qrStatus==='error'?'验证中断，可继续验证或重新扫码':'用 115 App 扫码并确认登录'}}</p><button v-if="qrStatus==='error'" class="outline" @click="resumeQR">继续验证</button></div>
            <details><summary>使用已有 Cookie 登录</summary><label>115 Cookie<textarea v-model="cookies" placeholder="UID=…; CID=…; SEID=…" autocomplete="off"/></label><button class="outline wide" :disabled="!cookies" @click="cookieLogin">验证并安全保存</button></details>
          </template><p v-if="state('p115').error" class="error-text" role="alert">{{state('p115').error}}</p><p v-if="state('p115').message" class="settings-success" role="status">{{state('p115').message}}</p>
        </fieldset></div>
        <form class="settings-card" @submit.prevent="saveConnections"><fieldset :disabled="state('connections').busy">
          <h2><Folder :size="20"/>OpenList 与 Emby</h2>
          <label>OpenList 地址<input v-model="draft.openlist_url" required/></label><label>OpenList 配置数据库<input v-model="draft.openlist_db"/></label><label>OpenList Token（可选）<input v-model="openlistToken" type="password" placeholder="留空保留已保存令牌或复用本机数据库令牌" autocomplete="off"/></label>
          <label>Emby 地址<input v-model="draft.emby_url" required/></label><label>Emby API Key（可选）<input v-model="embyToken" type="password" placeholder="留空保留已保存密钥；原生部署可复用系统凭据" autocomplete="off"/></label>
          <p v-if="state('connections').error" class="error-text" role="alert">{{state('connections').error}}</p><p v-if="state('connections').message" class="settings-success" role="status">{{state('connections').message}}</p><button class="primary" :disabled="!connectionsDirty">保存连接配置</button>
        </fieldset></form>
      </div>
    </div>

    <div v-show="tab==='network'" id="settings-panel-network" role="tabpanel" aria-labelledby="settings-tab-network">
      <form class="settings-card" @submit.prevent="saveGroup('network',networkFields)"><fieldset :disabled="state('network').busy">
        <h2><Folder :size="20"/>目录与网络</h2><p>新入库默认保留原文件名和目录层级。勾选“自动规范命名”后，电影按 TMDB 原语片名与清晰度命名，剧集按中文名（无中文时用原名）与季集号命名；缺少信息时暂停等待补全。存在活动任务时暂不能修改目录映射。</p>
        <div class="settings-field-grid"><label v-for="directory in directories" :key="directory.key">{{directory.name}}<input v-model="draft[directory.key]" :aria-label="directory.name" :required="directory.key!=='anime_target'"/></label></div>
        <label>代理地址（搜索源／Telegram／TMDB）<input v-model="draft.proxy" placeholder="socks5://127.0.0.1:7890" aria-label="代理地址"/></label><p>留空使用直连。更改后将用于后续请求，Telegram 连接会重新建立。</p>
        <label class="settings-checkbox"><input v-model="draft.magnet_use_proxy" type="checkbox"/><span>磁力解析使用应用代理</span></label>
        <p class="settings-hint">默认直连。HTTP 代理不支持 DHT 与 UDP，开启后冷门磁力可能无法解析。</p>
        <p v-if="state('network').error" class="error-text" role="alert">{{state('network').error}}</p><p v-if="state('network').message" class="settings-success" role="status">{{state('network').message}}</p>
        <div class="settings-actions"><span>{{networkDirty?'有未保存的更改':'已与保存配置同步'}}</span><button class="primary" :disabled="!networkDirty"><Loader2 v-if="state('network').busy" :size="17" class="spin"/><Check v-else :size="17"/>保存目录与网络</button></div>
      </fieldset></form>
    </div>

    <div v-show="tab==='security'" id="settings-panel-security" role="tabpanel" aria-labelledby="settings-tab-security" class="settings-grid">
      <form class="settings-card" @submit.prevent="changePassword"><fieldset :disabled="state('password').busy">
        <h2><LockKeyhole :size="20"/>修改访问密码</h2><p>修改的是 MediaLink 的个人访问密码。成功后所有设备都需要重新登录。</p>
        <label>当前密码<input v-model="currentPassword" type="password" autocomplete="current-password" minlength="8" maxlength="200" required/></label><label>新密码<input v-model="newPassword" type="password" autocomplete="new-password" minlength="8" maxlength="200" placeholder="8–200 个字符" required/></label><label>确认新密码<input v-model="confirmPassword" type="password" autocomplete="new-password" minlength="8" maxlength="200" required/></label>
        <p v-if="state('password').error" class="error-text" role="alert">{{state('password').error}}</p><button class="primary" :disabled="!currentPassword||!newPassword||!confirmPassword"><Loader2 v-if="state('password').busy" :size="17" class="spin"/><LockKeyhole v-else :size="17"/>修改密码并重新登录</button>
      </fieldset></form>
      <form class="settings-card public-access-settings" @submit.prevent="saveGroup('access',accessFields)"><fieldset :disabled="state('access').busy">
        <h2><Globe :size="20"/>公网访问</h2><p>开启后可通过允许的公网域名或 IP 登录 MediaLink。</p>
        <div class="public-access-toggle"><label id="public-access-label">允许公网访问</label><button type="button" role="switch" aria-labelledby="public-access-label" :aria-checked="!!draft.public_access_enabled" :class="{enabled:draft.public_access_enabled}" @click="draft.public_access_enabled=!draft.public_access_enabled"><span/></button></div>
        <label>允许的公网域名或 IP<textarea v-model="draft.public_access_hosts" placeholder="例如 media.example.com；多个地址用英文逗号或换行分隔" aria-label="允许的公网域名或 IP"/></label><p>不包含协议、端口或路径。保存后立即生效；端口映射或反向代理需单独配置。</p>
        <p v-if="state('access').error" class="error-text" role="alert">{{state('access').error}}</p><p v-if="state('access').message" class="settings-success" role="status">{{state('access').message}}</p><button class="primary" :disabled="!accessDirty"><Check :size="17"/>保存访问设置</button>
      </fieldset></form>
    </div>
  </section>
</template>
