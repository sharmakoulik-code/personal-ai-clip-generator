import {useEffect, useState} from 'react';
export type Warning = {code:string;severity:'info'|'warn'|'block';message:string;detail?:string};
export function usePreflight(videoId:string) {
  const [warnings,setWarnings]=useState<Warning[]>([]);
  useEffect(()=>{let live=true;setWarnings([]);fetch(`/api/videos/${videoId}/preflight`).then(r=>r.ok?r.json():null).then(v=>{if(live && Array.isArray(v?.warnings))setWarnings(v.warnings);}).catch(()=>{});return ()=>{live=false;};},[videoId]);
  return warnings;
}
export default function PreflightWarnings({warnings}:{warnings:Warning[]}) {
  if(!warnings.length)return null;
  return <section aria-label="Source preflight warnings"><h4>Source checks</h4>{warnings.map((w,i)=><p key={`${w.code}-${i}`} className={`preflight-${w.severity}`}><strong>{w.severity==='block'?'Blocked':w.severity==='warn'?'Warning':'Info'}:</strong> {w.message} {w.detail}</p>)}</section>;
}
