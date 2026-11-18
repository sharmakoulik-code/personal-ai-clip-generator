import { useCallback, useEffect, useRef, useState } from 'react';

type Clip = { id: string; start: number; end: number; title: string; selected: boolean };
type Render = { info?: { detector?: string; tracks?: number; speaker_switches?: number; speaker_status?: string } | null; clip_id: string; id: string; batch_id: string; project_id: number; video_id: string; clip_title: string; status: string; stage: string; progress: number | null; error: string | null; tracking_status: string | null; width: number; height: number };
type Props = { videoId: string; projectId: number; clips: Clip[]; initialClipId?: string };
const active = (r: Render) => r.status === 'pending' || r.status === 'running';
const stamp = (n: number) => `${String(Math.floor(n / 60)).padStart(2, '0')}:${String(Math.floor(n % 60)).padStart(2, '0')}`;
async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) { let text = `Request failed (${r.status})`; try { const j = await r.json(); if (typeof j.detail === 'string') text = j.detail; } catch { /* default */ } throw new Error(text); }
  return r.json();
}
const json = (x: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(x) });

export default function RenderPanel({ videoId, projectId, clips, initialClipId }: Props) {
  const [layout, setLayout] = useState<'smart' | 'center' | 'blur' | 'split'>('smart');
  const [resolution, setResolution] = useState(1920);
  const [quality, setQuality] = useState('balanced');
  const [horizontal, setHorizontal] = useState(0);
  const [vertical, setVertical] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [track, setTrack] = useState(true);
  const [engine, setEngine] = useState<'standard' | 'subject'>('standard');
  const [smoothTime, setSmoothTime] = useState(.55);
  const [speaker, setSpeaker] = useState(true);
  const [captions, setCaptions] = useState(false);
  const [captionStyle, setCaptionStyle] = useState('highlight');
  const [uppercase, setUppercase] = useState(false);
  const [pad, setPad] = useState(0);
  const [normalizeAudio, setNormalizeAudio] = useState(false);
  const [interval, setIntervalValue] = useState(1);
  const [ratio, setRatio] = useState(.67);
  const [clipId, setClipId] = useState('');
  const [renders, setRenders] = useState<Render[]>([]);
  const [error, setError] = useState('');
  const [previewError, setPreviewError] = useState(false);
  const [previewQuery, setPreviewQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const player = useRef<HTMLVideoElement>(null);
  const end = useRef<number | null>(null);
  const selected = clips.filter(c => c.selected);
  const clip = clips.find(c => c.id === clipId) || selected[0] || clips[0];
  useEffect(() => { if (initialClipId) setClipId(initialClipId); }, [initialClipId]);
  const refresh = useCallback(async () => {
    try { const rows = await request<Render[]>(`/api/renders?project_id=${projectId}`); setRenders(rows); }
    catch { setError('Could not load rendering jobs.'); }
  }, [projectId]);
  useEffect(() => { void refresh(); const t = window.setInterval(() => void refresh(), 2500); return () => window.clearInterval(t); }, [refresh]);
  const settings = { layout, resolution, quality, horizontal, vertical, zoom, track_faces: track, detect_interval: interval, split_ratio: ratio, tracking_engine: engine, smooth_time: smoothTime, active_speaker: engine === 'subject' && speaker, captions, caption_style: captionStyle, caption_uppercase: uppercase, pad, normalize_audio: normalizeAudio };
  const query = new URLSearchParams(Object.entries(settings).map(([k,v]) => [k, String(v)])).toString();
  useEffect(() => { const timer = window.setTimeout(() => { setPreviewError(false); setPreviewQuery(query); }, 300); return () => window.clearTimeout(timer); }, [clip?.id, query]);
  async function start(batch: boolean) {
    if (!clip) return;
    setBusy(true); setError('');
    try { await request(batch ? `/api/projects/${projectId}/render-batch` : `/api/clips/${clip.id}/render`, json(settings)); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not start render.'); }
    finally { setBusy(false); }
  }
  async function action(path: string, method: string) {
    setBusy(true); setError('');
    try { const r = await fetch(path, { method }); if (!r.ok) { const body = await r.json(); throw new Error(body.detail || 'Request failed.'); } await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Request failed.'); }
    finally { setBusy(false); }
  }
  const latestBatch = renders[0]?.batch_id;
  const batchRows = renders.filter(r => r.batch_id === latestBatch);
  const batchProgress = batchRows.length ? Math.round(batchRows.reduce((sum,r) => sum + (r.status === 'completed' ? 100 : r.progress || 0),0) / batchRows.length) : 0;
  const thisVideo = renders; // Project batches may include clips from several source videos.
  return <section className="render-panel" aria-label="Vertical video rendering">
    <h3>Vertical video rendering</h3>
    <p className="analysis-notice">Face and optional mouth-motion speaker framing are local heuristics, not reliable speaker identification. Check the preview and final video.</p>
    {error && <div className="error" role="alert"><span>{error}</span><button onClick={() => setError('')}>Dismiss</button></div>}
    {!clips.length ? <p>Analyze the video to create clips to render.</p> : <>
      <label>Clip to preview <select className="ingest-control" value={clip?.id || ''} onChange={e => setClipId(e.target.value)}>{clips.map(c => <option key={c.id} value={c.id}>{stamp(c.start)}–{stamp(c.end)} · {c.title}</option>)}</select></label>
      <div className="render-options">
        <label>Layout<select className="ingest-control" value={layout} onChange={e => setLayout(e.target.value as typeof layout)}><option value="smart">Smart face framing</option><option value="center">Center crop</option><option value="blur">Original with blurred background</option><option value="split">Split-screen (same source)</option></select></label>
        <label>Resolution<select className="ingest-control" value={resolution} onChange={e => setResolution(Number(e.target.value))}><option value={1920}>1080 × 1920</option><option value={1280}>720 × 1280</option></select></label>
        <label>Quality<select className="ingest-control" value={quality} onChange={e => setQuality(e.target.value)}><option value="high">High</option><option value="balanced">Balanced</option><option value="small">Smaller file</option></select></label>
        {(layout === 'smart' || layout === 'center') && <><label>Horizontal framing <input type="range" min="-1" max="1" step="0.05" value={horizontal} onChange={e => setHorizontal(Number(e.target.value))} /></label><label>Vertical framing <input type="range" min="-1" max="1" step="0.05" value={vertical} onChange={e => setVertical(Number(e.target.value))} /></label><label>Zoom <input type="range" min="1" max="2" step="0.05" value={zoom} onChange={e => setZoom(Number(e.target.value))} /></label><button className="secondary-btn" onClick={() => {setHorizontal(0); setVertical(0); setZoom(1); setLayout('smart'); setTrack(true);}}>Reset to automatic tracking</button></>}
        {layout === 'smart' && <><label><input type="checkbox" checked={track} onChange={e => setTrack(e.target.checked)} /> Track faces</label><label>Detection interval (seconds) <input className="ingest-control" type="number" min="0.5" max="5" step="0.5" value={interval} onChange={e => setIntervalValue(Number(e.target.value))} /></label></>}
        {layout === 'smart' && <><label>Tracking engine <select className="ingest-control" value={engine} onChange={e => setEngine(e.target.value as typeof engine)}><option value="standard">Standard face framing</option><option value="subject">Subject tracking</option></select></label>{engine === 'subject' && <><label>Camera smoothness <input type="range" min="0.15" max="2" step="0.05" value={smoothTime} onChange={e => setSmoothTime(Number(e.target.value))} /></label><label><input type="checkbox" checked={speaker} onChange={e => setSpeaker(e.target.checked)} /> Follow active speaker (heuristic)</label></>}</>}
        <label>Padding (seconds) <input className="ingest-control" type="number" min="0" max="1" step="0.05" value={pad} onChange={e => setPad(Number(e.target.value))} /></label><label><input type="checkbox" checked={normalizeAudio} onChange={e => setNormalizeAudio(e.target.checked)} /> Normalize audio loudness</label>
        <label><input type="checkbox" checked={captions} onChange={e => setCaptions(e.target.checked)} /> Burn in captions</label>{captions && <><label>Caption style <select className="ingest-control" value={captionStyle} onChange={e => setCaptionStyle(e.target.value)}><option value="highlight">Highlight</option><option value="classic">Classic</option><option value="minimal">Minimal</option></select></label><label><input type="checkbox" checked={uppercase} onChange={e => setUppercase(e.target.checked)} /> Uppercase captions</label></>}
        {layout === 'split' && <label>Main portion <input type="range" min="0.5" max="0.8" step="0.05" value={ratio} onChange={e => setRatio(Number(e.target.value))} /></label>}
      </div>
      <div className="render-previews"><div><strong>Crop preview</strong><p>First frame uses standard visual framing; subject tracking and captions appear in the final render.</p>{clip && previewQuery && !previewError ? <img className="crop-preview" alt="Representative crop preview" src={`/api/clips/${clip.id}/render-preview?${previewQuery}`} onError={() => setPreviewError(true)} /> : <p>Crop preview unavailable.</p>}</div><div><strong>Original video</strong><video ref={player} controls preload="metadata" src={`/api/videos/${videoId}/stream`} onTimeUpdate={() => { if (player.current && end.current !== null && player.current.currentTime >= end.current) { player.current.pause(); end.current = null; } }} /><button className="secondary-btn" onClick={() => { if (player.current && clip) { player.current.currentTime = clip.start; end.current = clip.end; void player.current.play().catch(() => setError('Unable to play source video in this browser.')); } }}>Play selected range</button></div></div>
      <div className="analysis-actions"><button className="primary-btn" disabled={busy} onClick={() => void start(false)}>Render selected clip</button><button className="secondary-btn" disabled={busy || !selected.length} onClick={() => void start(true)}>Render all selected clips in project</button></div>
    </>}
    {batchRows.length > 0 && <div className="job-progress"><strong>Latest batch: {batchRows.filter(r => r.status === 'completed').length}/{batchRows.length} completed</strong><progress max="100" value={batchProgress} /><span>{batchProgress}%</span></div>}
    {thisVideo.map(r => <article className="clip-card" key={r.id}><strong>{r.clip_title}</strong>{r.video_id !== videoId && <p>From another video in this project</p>}<p>{r.stage} · {r.status}{r.error ? ` · ${r.error}` : ''}</p>{r.progress != null && <div className="job-progress"><progress max="100" value={r.progress} /><span>{Math.round(r.progress)}%</span></div>}{r.tracking_status && <p>Framing: {r.tracking_status.replace(/_/g, ' ')}</p>}{r.info?.speaker_status === 'no_transcript' && <p>No timed speech in this clip; used visual tracking instead.</p>}{r.info?.speaker_status === 'heuristic' && <p>Speaker heuristic: {r.info.tracks} tracks, {r.info.speaker_switches} switches. Review the result.</p>}{r.status === 'completed' && <><video controls preload="metadata" src={`/api/renders/${r.id}/stream`} /><a className="secondary-btn" href={`/api/renders/${r.id}/download`}>Download MP4</a>{r.clip_id && <a className="secondary-btn" href={`/api/clips/${r.clip_id}/captions.srt`}>Download SRT</a>}</>}{active(r) && <button className="secondary-btn" disabled={busy} onClick={() => void action(`/api/renders/${r.id}/cancel`, 'POST')}>Cancel render</button>}{(r.status === 'failed' || r.status === 'cancelled') && <button className="secondary-btn" disabled={busy} onClick={() => void action(`/api/renders/${r.id}/retry`, 'POST')}>Retry render</button>}{!active(r) && <button className="delete-btn" disabled={busy} onClick={() => { if (window.confirm('Delete this render?')) void action(`/api/renders/${r.id}`, 'DELETE'); }}>Delete render</button>}</article>)}
  </section>;
}
