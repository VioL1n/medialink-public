import type {Resource} from './types'

const qualityOrder:Record<string,number>={
  '8K':6,'4K':5,'2160P':5,'1080P':4,'1080I':3,'720P':2,'480P':1
}

export function sortResources(resources:Resource[]):Resource[]{
  return [...resources].sort((a,b)=>
    (qualityOrder[b.resolution?.trim().toUpperCase()||'']||0)-
    (qualityOrder[a.resolution?.trim().toUpperCase()||'']||0)||
    (b.match?.score||0)-(a.match?.score||0)
  )
}

export function mergeResourceResults(current:Resource[],incoming:Resource[],final=false):Resource[]{
  const keys=new Set(incoming.map(r=>r.key))
  const map=new Map(current.filter(r=>!final||keys.has(r.key)).map(r=>[r.key,r]))
  for(const r of incoming){
    const old=map.get(r.key)
    map.set(r.key,old?{...old,...r,
      sources:[...old.sources,...r.sources.filter(s=>!old.sources.some(a=>a.url===s.url&&a.provider===s.provider))],
      matched_queries:[...new Set([...(old.matched_queries||[]),...(r.matched_queries||[])])]
    }:r)
  }
  return sortResources([...map.values()])
}
