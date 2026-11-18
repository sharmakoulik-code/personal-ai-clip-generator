import {useEffect,useState} from 'react';

type Metadata={titles:string[];description:string;hashtags:string[];provider:string};
export default function MetadataCard({clipId}:{clipId:string}) {
  const [data,setData]=useState<Metadata|null>(null); const [cloud,setCloud]=useState(false);
  const [tone,setTone]=useState('curious'); const [error,setError]=useState(''); const [busy,setBusy]=useState(false);
  useEffect(()=>{let live=true;fetch(`/api/clips/${clipId}/metadata`).then(r=>r.ok?r.json():null).then(v=>{if(live && Array.isArray(v?.titles))setData(v);}).catch(()=>{});return ()=>{live=false;};},[clipId]);
  async function generate() {
    setBusy(true);setError('');
    try {const r=await fetch(`/api/clips/${clipId}/metadata`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tone,cloud})});const v=await r.json();if(!r.ok)throw new Error(v.detail||'Metadata failed.');setData(v);}
    catch(e){setError(e instanceof Error?e.message:'Metadata failed.');}finally{setBusy(false);}
  }
  async function copy(text:string){try{await navigator.clipboard.writeText(text);}catch{setError('Could not copy to clipboard.');}}
  return <section aria-label="Metadata"><h4>Title, description and hashtags</h4>
    <label>Tone <select value={tone} onChange={e=>setTone(e.target.value)}><option value="curious">Curious</option><option value="direct">Direct</option><option value="funny">Funny</option></select></label>
    <label><input type="checkbox" checked={cloud} onChange={e=>setCloud(e.target.checked)}/> Use Cloud AI for metadata</label>
    <p>Cloud AI tries Groq first, then Google Gemini. Only the clip transcript and existing video title are sent when you opt in; never the video. Review suggestions.</p>
    <button className="secondary-btn" disabled={busy} onClick={()=>void generate()}>Generate metadata</button>
    {error && <p role="alert">{error}</p>}
    {data && <><p>Provider: {data.provider}</p>{data.titles.map((t,i)=><p key={i}>{t} <button onClick={()=>void copy(t)}>Copy title {i+1}</button></p>)}<p>{data.description} <button onClick={()=>void copy(data.description)}>Copy description</button></p><p>{data.hashtags.join(' ')} <button onClick={()=>void copy(data.hashtags.join(' '))}>Copy hashtags</button></p></>}
  </section>;
}
