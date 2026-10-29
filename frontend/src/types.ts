export type MediaSelection = {type:"movie"|"tv";id:number;season_number?:number}
export type Resource = {key:string;title:string;kind:string;link:string;password?:string;size?:string;resolution?:string;media?:MediaSelection;published?:string;seeders?:string;sources:{provider:string;name:string;url:string}[];matched_queries?:string[];match?:{status:string;score:number;explanation:string}}
export type SearchSource = {id:string;name:string;kind:string}
export type Media = {id:number;type:'movie'|'tv';title:string;chinese_title?:string;english_title?:string;original_title?:string;year?:number;poster?:string;backdrop?:string;rating?:number;votes:number;overview:string;overview_language?:string;genres?:string[];runtime?:number;creators?:string[];cast?:{name:string;character:string;portrait?:string}[];seasons?:{number:number;name:string;episodes?:number;year?:string}[];season_count?:number;episode_count?:number;updated?:number;stale?:boolean;warning?:string}
export type RequestFn = <T=any>(path:string,method?:string,body?:unknown,signal?:AbortSignal)=>Promise<T>
export type NamingFile = {id:string;source:string;target?:string|null;destination?:string|null;kind:'video'|'subtitle';status:string;basis?:string;llm_reason?:string;missing_fields?:string[];error?:string|null;resolution?:string|null;season_number?:number|null;episode_number?:number|null}
export type Naming = {version:number;revision?:number;review_required?:boolean;llm?:{status?:string|null;message?:string|null};media?:Pick<Media,'id'|'type'|'title'|'original_title'|'chinese_title'>|null;season_number?:number|null;resolution?:string|null;editable:boolean;locked:boolean;files:NamingFile[];warnings:string[];errors:string[];folder?:string|null;excluded?:NamingExcluded[];excluded_count?:number}
export type NamingExcluded = {id:string;source:string;size:number;kind:'video'|'subtitle'|'other';role:string;label:string;reason:string}

export type MagnetFile = {id:string;path:string;size:number;kind:'video'|'subtitle'|'other';role?:string;keep?:boolean;reason?:string}
export type MagnetPreview = {preview_id:string;infohash:string;root:string|null;files:MagnetFile[];total_size:number}
export type MagnetSelection = {preview_id:string;selected_ids:string[]}
export type NamingPreviewReference = {preview_id:string;revision:number}
export type NamingPreview = NamingPreviewReference & {status:'preparing'|'analyzing'|'ready'|'failed';error?:string|null;naming:Naming}
export type HomeItem = {id:string;detail:string;kind:'movie'|'tv'|'anime';title:string;english_title?:string|null;overview?:string;backdrop?:string|null;poster?:string|null;tags:string[];year?:number|null;counts?:{magnet:number;cloud:number;online:number};qualities?:string[];ratings?:{douban?:number;imdb?:number;mal?:number;tmdb?:number};heat?:string|null;status?:string|null;media?:{type:'movie'|'tv';id:number}|null;updated?:number}
export type HomeSection = {key:string;title:string;items:HomeItem[]}
export type HomeFeed = {source:'local';status:string;message:string;error?:string|null;warnings?:string[];updated?:number|null;refreshing:boolean;stale:boolean;featured:HomeItem[];sections:HomeSection[]}
export type AnimeItem = {id:number;title:string;poster?:string|null;updated?:string|null;media?:{type:'movie'|'tv';id:number}|null;subscribed?:boolean}
export type AnimeDay = {weekday:number;label:string;items:AnimeItem[]}
export type AnimeSeason = {year:number;season:string}
export type AnimeSchedule = {year?:number|null;season?:string|null;seasons:AnimeSeason[];days:AnimeDay[];updated?:number;stale:boolean;error?:string|null}
export type AnimeRelease = {title:string;hash:string;magnet:string;size?:string|null;published?:string|null;episode?:number|null;resolution?:string|null}
export type AnimeBangumi = {id:number;title:string;weekday?:number|null;start?:string|null;bgm_id?:number|null;poster?:string|null;subgroups:{id:number;name:string;releases:AnimeRelease[]}[];media?:MediaSelection|null;target:string}
export type SubscriptionRules = {resolution:string|null;include:string[];exclude:string[]}
export type SubscriptionEpisode = {episode:number;title:string;task_id?:string|null;state?:string|null;progress?:number|null;error?:string|null;created:number}
export type Subscription = {id:string;bangumi_id:number;subgroup_id?:number|null;subgroup_name?:string|null;title:string;poster?:string|null;media_type:'movie'|'tv';media_id:number;season_number?:number|null;target:string;rules:SubscriptionRules;backfill:number;initialized:number;enabled:number;created:number;checked:number;next_check:number;error?:string|null;failures:number;episodes:SubscriptionEpisode[]}
