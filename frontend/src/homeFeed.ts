import type {HomeItem} from './types'

export type Chip = {kind:'magnet'|'cloud'|'online'|'quality';label:string;title:string}
export type Rating = {kind:'douban'|'imdb'|'mal'|'tmdb'|'heat';label:string;title:string}

const KINDS:Record<HomeItem['kind'],string>={movie:'电影',tv:'剧集',anime:'动漫'}

export function kindLabel(item:HomeItem){return KINDS[item.kind]||'影视'}

/** Resource counts first, then quality labels, followed by quality badges. */
export function resourceChips(item:HomeItem):Chip[]{
  const counts=item.counts||{magnet:0,cloud:0,online:0}
  const chips:Chip[]=[]
  if(counts.magnet>0)chips.push({kind:'magnet',label:String(counts.magnet),title:`${counts.magnet} 个磁力资源`})
  if(counts.cloud>0)chips.push({kind:'cloud',label:String(counts.cloud),title:`${counts.cloud} 个网盘分享`})
  if(counts.online>0)chips.push({kind:'online',label:String(counts.online),title:`${counts.online} 个在线播放源`})
  const quality=(item.qualities||[]).join(' ')
  if(quality)chips.push({kind:'quality',label:quality,title:'最高画质 '+quality})
  return chips
}

export function ratingList(item:HomeItem):Rating[]{
  const ratings=item.ratings||{}, list:Rating[]=[]
  if(ratings.douban)list.push({kind:'douban',label:ratings.douban.toFixed(1),title:'豆瓣评分'})
  if(ratings.imdb)list.push({kind:'imdb',label:ratings.imdb.toFixed(1),title:'IMDb 评分'})
  if(ratings.mal)list.push({kind:'mal',label:ratings.mal.toFixed(1),title:'MyAnimeList 评分'})
  if(ratings.tmdb)list.push({kind:'tmdb',label:ratings.tmdb.toFixed(1),title:'TMDB 评分'})
  if(item.heat)list.push({kind:'heat',label:item.heat,title:'评分人数'})
  return list
}

/** Linked titles open the detail page; others fall back to a resource search by title. */
export function openTarget(item:HomeItem):{media:{type:'movie'|'tv';id:number}}|{search:string}{
  return item.media?{media:item.media}:{search:item.title}
}

export function updatedLabel(updated:number|null|undefined,now=Date.now()){
  if(!updated)return ''
  const minutes=Math.max(0,Math.floor((now-updated*1000)/60000))
  if(minutes<1)return '刚刚更新'
  if(minutes<60)return `${minutes} 分钟前更新`
  const hours=Math.floor(minutes/60)
  return hours<24?`${hours} 小时前更新`:new Date(updated*1000).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})+' 更新'
}
