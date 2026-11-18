import {useEffect, useRef, useState} from 'react';

type Clip = {id:string; start:number; end:number; title:string; selected:boolean};
type Rect = {t:number;x:number;y:number;w:number;h:number};
type Preview = {width:number;height:number;layout:string;source_duration:number;path:Rect[];canvas_width?:number;canvas_height?:number;video_rect?:{x:number;y:number;w:number;h:number};letterbox_bg?:string};
type Constants = {safe_zone:{top:number;bottom:number;left:number;right:number}};
type Props = {clip:Clip; videoId:string; query:string; xOffset:number; setXOffset:(n:number)=>void; onUpdate:(c:Clip)=>void};

export default function ReviewPanel({clip,videoId,query,xOffset,setXOffset,onUpdate}:Props) {
  const [preview,setPreview]=useState<Preview|null>(null);
  const [safe,setSafe]=useState(false);
  const [constants,setConstants]=useState<Constants|null>(null);
  const [time,setTime]=useState(clip.start);
  const [error,setError]=useState('');
  const video=useRef<HTMLVideoElement>(null);
  useEffect(()=> { let live=true; fetch('/api/shorts/constants').then(r=>r.ok?r.json():null).then(v=>{if(live && v?.safe_zone)setConstants(v);}).catch(()=>{}); return ()=>{live=false;}; },[]);
  useEffect(()=> {
    let live=true; const timer=window.setTimeout(()=>{
      fetch(`/api/clips/${clip.id}/preview-path?${query}`).then(r=>r.ok?r.json():null).then(v=>{if(live)setPreview(Array.isArray(v?.path) ? v : null);}).catch(()=>{if(live)setPreview(null);});
    },300);
    return ()=>{live=false;window.clearTimeout(timer);};
  },[clip.id,clip.start,clip.end,query]);
  useEffect(()=> {setTime(clip.start); if(video.current)video.current.currentTime=clip.start;},[clip.id,clip.start]);
  async function update(body:unknown, revert=false) {
    setError('');
    try {
      const r=await fetch(`/api/clips/${clip.id}${revert?'/revert':''}`,revert?{method:'POST'}:{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(!r.ok) {const b=await r.json();throw new Error(b.detail || 'Could not update clip.');}
      onUpdate(await r.json());
    } catch(e) {setError(e instanceof Error?e.message:'Could not update clip.');}
  }
  function nudge(which:'start'|'end', delta:number) {
    const duration=preview?.source_duration ?? clip.end;
    const value=which==='start' ? Math.max(0,Math.min(clip.end-3,clip.start+delta)) : Math.max(clip.start+3,Math.min(duration,clip.end+delta));
    void update({[which]:Math.round(value*10)/10});
  }
  const rect=preview?.path.reduce<Rect|undefined>((best,p)=>!best || Math.abs(p.t-time)<Math.abs(best.t-time)?p:best,undefined);
  const box=preview?.layout==='letterbox' && preview.video_rect && preview.canvas_width && preview.canvas_height ? preview.video_rect : null;
  const cw=preview?.canvas_width || 1, ch=preview?.canvas_height || 1;
  return <section aria-label="Clip review" className="clip-review">
    <h4>Review clip</h4><p>Preview is a local framing estimate; check the final render.</p>
    {error && <p role="alert">{error}</p>}
    <div className="review-video" style={{position:'relative',aspectRatio:box ? `${cw}/${ch}` : preview ? `${preview.width}/${preview.height}` : '16/9',backgroundColor:box ? preview?.letterbox_bg : undefined}}>
      <video ref={video} controls preload="metadata" src={`/api/videos/${videoId}/stream`} style={box ? {position:'absolute',left:`${100*box.x/cw}%`,top:`${100*box.y/ch}%`,width:`${100*box.w/cw}%`,height:`${100*box.h/ch}%`} : {width:'100%',height:'100%'}} onLoadedMetadata={()=>{if(video.current)video.current.currentTime=clip.start;}} onTimeUpdate={()=>{if(video.current){if(video.current.currentTime>=clip.end || video.current.currentTime<clip.start) video.current.currentTime=clip.start; setTime(video.current.currentTime);}}}/>
      {preview && rect && <div aria-label="Crop overlay" data-layout={preview.layout} style={{pointerEvents:'none',position:'absolute',border:'2px solid #50e3c2',boxSizing:'border-box',left:`${box ? 100*box.x/cw : 100*rect.x/preview.width}%`,top:`${box ? 100*box.y/ch : 100*rect.y/preview.height}%`,width:`${box ? 100*box.w/cw : 100*rect.w/preview.width}%`,height:`${box ? 100*box.h/ch : 100*rect.h/preview.height}%`}}>
        {!box && safe && constants && <div aria-label="Caption safe zone" style={{position:'absolute',border:'1px dashed white',top:`${100*constants.safe_zone.top}%`,bottom:`${100*constants.safe_zone.bottom}%`,left:`${100*constants.safe_zone.left}%`,right:`${100*constants.safe_zone.right}%`}}/>}
      </div>}
      {box && safe && constants && <div aria-label="Caption safe zone" style={{pointerEvents:'none',position:'absolute',border:'1px dashed white',top:`${100*constants.safe_zone.top}%`,bottom:`${100*constants.safe_zone.bottom}%`,left:`${100*constants.safe_zone.left}%`,right:`${100*constants.safe_zone.right}%`}}/>}
    </div>
    <label><input type="checkbox" checked={safe} onChange={e=>setSafe(e.target.checked)}/> Show safe zone</label>
    <p>Start {clip.start.toFixed(1)} s · End {clip.end.toFixed(1)} s</p>
    <div className="analysis-actions">{(['start','end'] as const).flatMap(which=>[-.5,-.1,.1,.5].map(delta=><button key={`${which}${delta}`} className="secondary-btn" onClick={()=>nudge(which,delta)}>{which==='start'?'Start':'End'} {delta>0?'+':''}{delta.toFixed(1)} s</button>))}</div>
    <label>Crop x offset <input type="range" min="-.35" max=".35" step=".01" value={xOffset} onChange={e=>setXOffset(Number(e.target.value))}/></label>
    <button className="secondary-btn" onClick={()=>void update({},true)}>Revert to AI pick</button>
  </section>;
}
