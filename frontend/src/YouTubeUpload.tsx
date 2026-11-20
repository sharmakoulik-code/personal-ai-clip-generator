import {useCallback, useEffect, useState} from 'react';

type Render = {id: string; clip_id: string; clip_title: string; status: string};
type History = {render_id: string; status: string; video_id: string | null; publish_at: string | null};
type Status = {configured: boolean; connected: boolean; connecting: boolean};
type Entry = {selected: boolean; title: string; description: string; schedule: string; audience: '' | 'yes' | 'no'};
const headers = {'Content-Type':'application/json', 'X-Clip-Studio':'youtube-upload'};
async function api<T>(url: string, method = 'GET', data?: unknown): Promise<T> {
  const r = await fetch(url, {method, headers, ...(data !== undefined ? {body: JSON.stringify(data)} : {})});
  const result = await r.json();
  if (!r.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Request failed. Check your inputs.');
  return result;
}
export default function YouTubeUpload({projectId, renders}: {projectId: number; renders: Render[]}) {
  const [status,setStatus] = useState<Status | null>(null);
  const [history,setHistory] = useState<History[]>([]);
  const [entries,setEntries] = useState<Record<string, Entry>>({});
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState('');
  const [message,setMessage] = useState('');
  const completed = renders.filter(r => r.status === 'completed');
  const refresh = useCallback(async () => {
    try {
      const [s,h] = await Promise.all([api<Status>('/api/youtube/status'), api<History[]>(`/api/youtube/uploads?project_id=${projectId}`)]);
      if (!Array.isArray(h) || typeof s?.connected !== 'boolean' || typeof s?.configured !== 'boolean') throw new Error('Invalid upload status');
      setStatus(s); setHistory(h);
    } catch { setError('YouTube upload is available when the local app is opened at http://127.0.0.1:5173.'); }
  }, [projectId]);
  useEffect(() => { void refresh(); const timer = window.setInterval(() => void refresh(), 5000); return () => window.clearInterval(timer); }, [refresh]);
  function setEntry(id: string, patch: Partial<Entry>) {
    const render = completed.find(r=>r.id===id);
    setEntries(current => ({...current, [id]: {...(current[id] || {selected:false,title:render?.clip_title || '',description:'',schedule:'',audience:''}), ...patch}}));
  }
  async function select(render: Render, selected: boolean) {
    const exists = !!entries[render.id];
    setEntry(render.id, {selected});
    if (selected && !exists && render.clip_id) {
      try {
        const r = await fetch(`/api/clips/${render.clip_id}/metadata`);
        if (r.ok) {
          const m = await r.json();
          setEntries(current => {
            const item = current[render.id];
            if (!item) return current;
            return {...current, [render.id]: {...item, title: item.title === render.clip_title && typeof m.titles?.[0] === 'string' ? m.titles[0] : item.title, description: item.description || (typeof m.description === 'string' ? m.description : '')}};
          });
        }
      } catch { /* User can enter metadata manually. */ }
    }
  }
  async function connect() {
    const tab = window.open('', '_blank');
    setError('');
    if (!tab) { setError('Allow pop-ups and try connecting again.'); return; }
    try { const result = await api<{url:string}>('/api/youtube/connect','POST'); tab.location.href = result.url; await refresh(); }
    catch(e) { tab?.close(); setError(e instanceof Error ? e.message : 'Could not connect.'); }
  }
  async function upload() {
    const chosen = completed.filter(r=>entries[r.id]?.selected);
    if (!chosen.length) {setError('Select at least one completed render.'); return;}
    for (const r of chosen) {
      const e = entries[r.id];
      if (!e.title.trim() || e.title.length > 100 || e.description.length > 5000 || !e.audience || !e.schedule || !Number.isFinite(new Date(e.schedule).getTime()) || new Date(e.schedule).getTime() <= Date.now()) {
        setError('For every selected video, enter a title, choose an audience and choose a future publication time.'); return;
      }
    }
    if (!window.confirm(`Upload ${chosen.length} selected rendered video(s) to YouTube? They will be private until their chosen public time. Only upload videos you have rights to.`)) return;
    setBusy(true); setError(''); setMessage('');
    let count = 0;
    try {
      for (const r of chosen) {
        const previous = history.find(h=>h.render_id===r.id);
        let acknowledge = false;
        if (previous && previous.status !== 'failed') {
          if (previous.status === 'uploading') throw new Error('An upload for this render is already in progress.');
          acknowledge = window.confirm(`This render was already uploaded or the previous result is unknown. Check YouTube Studio first. Uploading again could create a duplicate. Upload it again?`);
          if (!acknowledge) throw new Error('Stopped to avoid a possible duplicate.');
        }
        const e = entries[r.id];
        setMessage(`Uploading ${count + 1} of ${chosen.length}… Keep this page and the app open.`);
        const result = await api<{status:string}>('/api/youtube/upload','POST',{render_id:r.id,title:e.title.trim(),description:e.description,publish_at:new Date(e.schedule).toISOString(),made_for_kids:e.audience==='yes',retry_acknowledged:acknowledge});
        count++; setEntry(r.id,{selected:false}); await refresh();
        if (result.status === 'schedule_unconfirmed') throw new Error('YouTube did not confirm the schedule. Check YouTube Studio before continuing.');
      }
      setMessage(`${count} video(s) uploaded. Check their schedule and processing in YouTube Studio.`);
    } catch(e) {setError(`${count} uploaded. ${e instanceof Error ? e.message : 'Upload failed.'} Check YouTube Studio before retrying.`); await refresh();}
    finally {setBusy(false);}
  }
  if (!completed.length) return null;
  return <section className="youtube-upload" aria-label="Upload to YouTube">
    <h4>Schedule rendered videos on YouTube</h4>
    <p>Only selected completed renders, their titles, descriptions, audience and schedule are sent to YouTube when you confirm. Uploads are private until the chosen public time. Keep the app open until uploads finish; YouTube handles publication afterward.</p>
    <p>Times use your computer's local time zone. Review copyright, titles and settings in YouTube Studio.</p>
    {!status?.configured && <p>Set up your Google OAuth desktop client first. See the YouTube upload section in the README.</p>}
    <p>Account: {status?.connected ? 'Connected for this app session' : status?.connecting ? 'Waiting for Google sign-in' : 'Not connected'}</p>
    {!status?.connected ? <button type="button" className="secondary-btn" disabled={!status?.configured || status.connecting || busy} onClick={()=>void connect()}>Connect YouTube</button> : <button type="button" className="secondary-btn" disabled={busy} onClick={()=>void api('/api/youtube/disconnect','POST').then(refresh)}>Disconnect</button>}
    {completed.map(r=>{
      const e = entries[r.id]; const h = history.find(x=>x.render_id===r.id);
      return <div key={r.id} className="youtube-entry">
        <label><input type="checkbox" disabled={busy} checked={!!e?.selected} onChange={event=>void select(r,event.target.checked)}/> {r.clip_title}</label>
        {h && <p>Previous upload: {h.status}{h.video_id && <> · <a href={`https://www.youtube.com/watch?v=${h.video_id}`} target="_blank" rel="noreferrer">View on YouTube</a></>} {h.publish_at && `· ${new Date(h.publish_at).toLocaleString()}`}</p>}
        {e?.selected && <div className="youtube-fields">
          <label>Title <input className="ingest-control" maxLength={100} value={e.title} disabled={busy} onChange={event=>setEntry(r.id,{title:event.target.value})}/></label>
          <label>Description <textarea className="ingest-control" maxLength={5000} value={e.description} disabled={busy} onChange={event=>setEntry(r.id,{description:event.target.value})}/></label>
          <label>Publish publicly at (local time) <input className="ingest-control" type="datetime-local" value={e.schedule} disabled={busy} onChange={event=>setEntry(r.id,{schedule:event.target.value})}/></label>
          <label>Made for kids? <select className="ingest-control" value={e.audience} disabled={busy} onChange={event=>setEntry(r.id,{audience:event.target.value as Entry['audience']})}><option value="">Choose audience</option><option value="yes">Yes, made for kids</option><option value="no">No, not made for kids</option></select></label>
        </div>}
      </div>;
    })}
    <button type="button" className="primary-btn" disabled={busy || !status?.connected} onClick={()=>void upload()}>Upload and schedule selected</button>
    {message && <p role="status">{message}</p>}{error && <p role="alert">{error}</p>}
  </section>;
}
