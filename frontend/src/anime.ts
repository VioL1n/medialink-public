import type {AnimeDay,SubscriptionEpisode,SubscriptionRules} from './types'

// Mikan numbers days 0 (Sunday) to 6, then 7 for films and 8 for OVAs; the week starts on Monday here.
const ORDER=[1,2,3,4,5,6,0,7,8]

export function orderedDays(days:AnimeDay[]){return [...days].sort((a,b)=>ORDER.indexOf(a.weekday)-ORDER.indexOf(b.weekday))}

export function todayWeekday(now=new Date()){return now.getDay()}

export function defaultDay(days:AnimeDay[],now=new Date()){
  const today=days.find(day=>day.weekday===todayWeekday(now)&&day.items.length)
  return today?today.weekday:(orderedDays(days).find(day=>day.items.length)?.weekday??todayWeekday(now))
}

export function splitWords(value:string){return [...new Set(value.split(/[\s,，]+/).map(word=>word.trim()).filter(Boolean))]}

export function ruleSummary(rules:SubscriptionRules){
  const parts=[rules.resolution||'不限清晰度']
  if(rules.include.length)parts.push('包含 '+rules.include.join('、'))
  if(rules.exclude.length)parts.push('排除 '+rules.exclude.join('、'))
  return parts.join(' · ')
}

export type EpisodeStatus = {label:string;tone:'done'|'active'|'attention'|'skipped'}

export function episodeStatus(episode:SubscriptionEpisode):EpisodeStatus{
  if(!episode.task_id)return {label:'已跳过',tone:'skipped'}
  if(episode.state==='completed')return {label:'已入库',tone:'done'}
  if(episode.state==='canceled')return {label:'已取消',tone:'skipped'}
  if(episode.state==='needs_attention'||episode.state==='naming_review')return {label:'需要处理',tone:'attention'}
  return {label:'入库中',tone:'active'}
}

export function checkLabel(timestamp:number,now=Date.now()){
  if(!timestamp)return '尚未检查'
  const minutes=Math.round((timestamp*1000-now)/60000)
  if(Math.abs(minutes)<1)return '刚刚'
  if(minutes>0)return minutes<60?`${minutes} 分钟后`:`${Math.round(minutes/60)} 小时后`
  return -minutes<60?`${-minutes} 分钟前`:-minutes<1440?`${Math.round(-minutes/60)} 小时前`:new Date(timestamp*1000).toLocaleDateString('zh-CN')
}
