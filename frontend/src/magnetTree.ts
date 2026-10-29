import type {MagnetFile} from './types'
export type MagnetNode = {name:string;path:string;file?:MagnetFile;children:MagnetNode[];ids:string[];size:number}
export function magnetTree(files:MagnetFile[]):MagnetNode[]{
  const roots:MagnetNode[]=[], nodes=new Map<string,MagnetNode>()
  for(const file of files){
    let children=roots, path=''
    const parts=file.path.split('/')
    for(let i=0;i<parts.length;i++){
      path=path?path+'/'+parts[i]:parts[i]!
      let node=nodes.get(path)
      if(!node){node={name:parts[i]!,path,children:[],ids:[],size:0};nodes.set(path,node);children.push(node)}
      node.ids.push(file.id);node.size+=file.size
      if(i===parts.length-1)node.file=file
      children=node.children
    }
  }
  return roots
}
export function toggleFiles(chosen:string[], ids:string[], checked:boolean):string[]{
  const selected=new Set(chosen)
  for(const id of ids)if(checked)selected.add(id);else selected.delete(id)
  return [...selected]
}
// Server-side curation leaves out ads, samples and unrelated files; older cached previews lack it.
export function defaultMagnetFiles(files:MagnetFile[]):string[]{
  return files.filter(file=>file.keep??(file.kind==='video'||file.kind==='subtitle')).map(file=>file.id)
}
export const roleLabels:Record<string,string>={ad:'广告',extra:'花絮',sample:'样片',other:'非影视'}
export function fileBytes(bytes:number):string{
  if(bytes<1024)return bytes+' B'
  const units=['KB','MB','GB','TB'];let value=bytes/1024,index=0
  while(value>=1024&&index<units.length-1){value/=1024;index++}
  return value.toFixed(value>=10?1:2)+' '+units[index]
}
