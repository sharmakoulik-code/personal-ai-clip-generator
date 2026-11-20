import ReviewPanel from './ReviewPanel';
import PreflightWarnings, {usePreflight} from './PreflightWarnings';
import MetadataCard from './MetadataCard';
import YouTubeUpload from './YouTubeUpload';
import { useCallback, useEffect, useRef, useState } from 'react';

type Clip = { id: string; start: number; end: number; title: string; selected: boolean };
type Render = { settings?: { preset?: string; captions?: boolean; caption_safe_zone?: boolean; loudness_lufs?: number }; info?: { detector?: string; tracks?: number; speaker_switches?: number; speaker_status?: string; layout_used?: string; removed_seconds?: number; duration?: number; loudness?: {before:number|null;after:number|null} } | null; clip_id: string; id: string; batch_id: string; project_id: number; video_id: string; clip_title: string; status: string; stage: string; progress: number | null; error: string | null; tracking_status: string | null; width: number; height: number };
type Props = { videoId: string; projectId: number; clips: Clip[]; initialClipId?: string; onClipUpdated?: (c: Clip) => void; shortsMode?: boolean };
const active = (r: Render) => r.status === 'pending' || r.status === 'running';
const stamp = (n: number) => `${String(Math.floor(n / 60)).padStart(2, '0')}:${String(Math.floor(n % 60)).padStart(2, '0')}`;
async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) { let text = `Request failed (${r.status})`; try { const j = await r.json(); if (typeof j.detail === 'string') text = j.detail; } catch { /* default */ } throw new Error(text); }
  return r.json();
}
const json = (x: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(x) });

export default function RenderPanel({ videoId, projectId, clips, initialClipId, onClipUpdated, shortsMode=false }: Props) {
  const [shortsPreset,setShortsPreset] = useState(shortsMode);
  const [tighten,setTighten] = useState(true);
  const [loudness,setLoudness] = useState(-14);
  const [sharpen,setSharpen] = useState(.35);
  const warnings=usePreflight(videoId);
  const noAudio=warnings.some(w=>w.code==='no_audio' && w.severity==='block');
  const [copyrightVisible,setCopyrightVisible]=useState(false);
  const [layout, setLayout] = useState<'auto' | 'follow' | 'fit_blur' | 'stack' | 'letterbox' | 'smart' | 'center' | 'blur' | 'split'>('auto');
  function chooseLayout(next: typeof layout) {
    setLayout(next);
    setCaptionPosition(next === 'letterbox' ? 'on_video' : 'safe_zone');
  }
  const [letterboxBg,setLetterboxBg]=useState('#000000');
  const [letterboxBias,setLetterboxBias]=useState(.02);
  const [letterboxEdge,setLetterboxEdge]=useState(false);
  const [captionPosition,setCaptionPosition]=useState<'on_video'|'safe_zone'>('safe_zone');
  const validLetterboxColor=letterboxBg.length===7 && /^#[0-9a-fA-F]{6}$/.test(letterboxBg);
  const [resolution, setResolution] = useState(1920);
  const [quality, setQuality] = useState('balanced');
  const [xOffset, setXOffset] = useState(0);
  const [yOffset, setYOffset] = useState(0);
  const [startOffset, setStartOffset] = useState(0);
  const [endOffset, setEndOffset] = useState(0);
  const [clipOverride, setClipOverride] = useState<Clip | null>(null);
  const [horizontal, setHorizontal] = useState(0);
  const [vertical, setVertical] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [track, setTrack] = useState(true);
  const [engine, setEngine] = useState<'standard' | 'subject'>('standard');
  const [smoothTime, setSmoothTime] = useState(.55);
  const [speaker, setSpeaker] = useState(true);
  const [stackTwo, setStackTwo] = useState(false);
  const [captions, setCaptions] = useState(shortsMode);
  const [forceCaptions, setForceCaptions] = useState(false);
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
  const chosen = clips.find(c => c.id === clipId) || selected[0] || clips[0];
  const clip = clipOverride?.id === chosen?.id ? clipOverride : chosen;
  useEffect(() => { if (initialClipId) setClipId(initialClipId); }, [initialClipId]);
  const refresh = useCallback(async () => {
    try { const rows = await request<Render[]>(`/api/renders?project_id=${projectId}`); setRenders(rows); }
    catch { setError('Could not load rendering jobs.'); }
  }, [projectId]);
  useEffect(() => { void refresh(); const t = window.setInterval(() => void refresh(), 2500); return () => window.clearInterval(t); }, [refresh]);
  const settings = { preset: shortsPreset ? 'shorts' : 'legacy', tighten, loudness_lufs:loudness, sharpen, caption_safe_zone:true, caption_position:captionPosition, letterbox_bg:validLetterboxColor ? letterboxBg : '#000000', letterbox_vertical_bias:letterboxBias, letterbox_edge:letterboxEdge, layout, resolution, quality, x_offset:xOffset, y_offset:yOffset, start_offset:startOffset, end_offset:endOffset, horizontal, vertical, zoom, track_faces: track, detect_interval: interval, split_ratio: ratio, tracking_engine: engine, smooth_time: smoothTime, active_speaker: (engine === 'subject' || layout === 'auto' || layout === 'follow') && speaker, stack_two_speakers: stackTwo, captions, force_captions: forceCaptions, caption_style: captionStyle, caption_uppercase: uppercase, pad, normalize_audio: normalizeAudio };
  const query = new URLSearchParams(Object.entries(settings).map(([k,v]) => [k, String(v)])).toString();
  useEffect(() => { const timer = window.setTimeout(() => { setPreviewError(false); setPreviewQuery(query); }, 300); return () => window.clearTimeout(timer); }, [clip?.id, query]);
  async function start(batch: boolean) {
    if (!clip || (layout==='letterbox' && !validLetterboxColor)) return;
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
  function notice() {
    try { if(sessionStorage.getItem('copyright-ack')!=='yes') setCopyrightVisible(true); }
    catch { setCopyrightVisible(true); }
  }
  function acknowledge() { try {sessionStorage.setItem('copyright-ack','yes');} catch { /* unavailable storage */ } setCopyrightVisible(false); }
  const latestBatch = renders[0]?.batch_id;
  const batchRows = renders.filter(r => r.batch_id === latestBatch);
  const batchProgress = batchRows.length ? Math.round(batchRows.reduce((sum,r) => sum + (r.status === 'completed' ? 100 : r.progress || 0),0) / batchRows.length) : 0;
  const thisVideo = renders; // Project batches may include clips from several source videos.
  return <section className="render-panel" aria-label="Vertical video rendering">
    <h3>Vertical video rendering</h3>
    <p className="analysis-notice">Face and optional mouth-motion speaker framing are local heuristics, not reliable speaker identification. Check the preview and final video.</p>
    <PreflightWarnings warnings={warnings} />
    {copyrightVisible && <div role="status" className="analysis-notice">If your video contains music or footage you don't own, YouTube may block, mute or claim it. Only upload content you have the rights to. <button onClick={acknowledge}>Got it</button></div>}
    {error && <div className="error" role="alert"><span>{error}</span><button onClick={() => setError('')}>Dismiss</button></div>}
    {!clips.length ? <p>Analyze the video to create clips to render.</p> : <>
      <label>Clip to preview <select className="ingest-control" value={clip?.id || ''} onChange={e => setClipId(e.target.value)}>{clips.map(c => <option key={c.id} value={c.id}>{stamp(c.start)}–{stamp(c.end)} · {c.title}</option>)}</select></label>
      <label><input type="checkbox" checked={shortsPreset} onChange={e=>{const enabled=e.target.checked;setShortsPreset(enabled);if(enabled){setCaptions(true);setTighten(true);setLoudness(-14);}}}/> YouTube Shorts preset</label>
      <fieldset className="layout-choice">
        <legend>Choose your video layout</legend>
        <p>Both export a vertical 9:16 video. Choose how the original frame fits inside it.</p>
        <div className="layout-choice-grid">
          <button type="button" className="layout-choice-card" aria-label="9:16 crop" aria-pressed={layout === 'follow'} onClick={() => chooseLayout('follow')}>
            <span className="layout-choice-icon layout-crop-icon" aria-hidden="true"><span /></span>
            <span><strong>9:16 crop</strong><small>Fill the screen with a portrait crop that follows the subject. The sides may be cut off.</small></span>
          </button>
          <button type="button" className="layout-choice-card" aria-label="Cinematic bars" aria-pressed={layout === 'letterbox'} onClick={() => chooseLayout('letterbox')}>
            <span className="layout-choice-icon layout-bars-icon" aria-hidden="true"><span /></span>
            <span><strong>Cinematic bars</strong><small>Keep the entire original frame, with solid bars around it. No face-following or crop.</small></span>
          </button>
        </div>
        {layout !== 'follow' && layout !== 'letterbox' && <p>Currently using an Advanced layout. Select a style above or change the Layout setting below.</p>}
      </fieldset>
      <details open><summary>Advanced</summary><div className="render-options">
        <label>Sharpen <input type="range" min="0" max="1" step=".05" value={sharpen} onChange={e=>setSharpen(Number(e.target.value))}/></label>
        <label>Target loudness (LUFS) <input type="number" min="-30" max="-5" value={loudness} onChange={e=>setLoudness(Number(e.target.value))}/></label>
        <label><input type="checkbox" checked={tighten} onChange={e=>setTighten(e.target.checked)}/> Tighten pauses and fillers</label>
        <label>Layout<select className="ingest-control" value={layout} onChange={e => chooseLayout(e.target.value as typeof layout)}><option value="auto">Auto Shorts framing</option><option value="follow">Follow subject</option><option value="fit_blur">Full frame with blurred background</option><option value="stack">Stack two speakers</option><option value="letterbox">Cinematic bars</option><option value="smart">Smart face framing</option><option value="center">Center crop</option><option value="blur">Original with blurred background</option><option value="split">Split-screen (same source)</option></select></label>
        {layout === 'letterbox' && <>
          <span className="letterbox-schematic" role="img" aria-label="Cinematic bars schematic"><span /></span>
          <label>Background colour <input type="text" value={letterboxBg} onChange={e=>setLetterboxBg(e.target.value)} aria-invalid={!validLetterboxColor} placeholder="#000000" /></label>
          <label>Pick background colour <input type="color" value={validLetterboxColor ? letterboxBg : '#000000'} onChange={e=>setLetterboxBg(e.target.value)} /></label>
          {!validLetterboxColor && <p role="alert">Use a six-digit hex colour like #000000.</p>}
          <label>Vertical position <input type="range" min="-.15" max=".15" step=".01" value={letterboxBias} onChange={e=>setLetterboxBias(Number(e.target.value))}/></label>
          <label><input type="checkbox" checked={letterboxEdge} onChange={e=>setLetterboxEdge(e.target.checked)}/> Soft video edge</label>
          <label>Caption position <select value={captionPosition} onChange={e=>setCaptionPosition(e.target.value as typeof captionPosition)}><option value="on_video">On video</option><option value="safe_zone">Safe zone</option></select></label>
        </>}
        <label>Resolution<select className="ingest-control" value={resolution} onChange={e => setResolution(Number(e.target.value))}><option value={1920}>1080 × 1920</option><option value={1280}>720 × 1280</option></select></label>
        <label>Quality<select className="ingest-control" value={quality} onChange={e => setQuality(e.target.value)}><option value="high">High</option><option value="balanced">Balanced</option><option value="small">Smaller file</option></select></label>
        {(layout === 'smart' || layout === 'center' || layout === 'follow' || layout === 'auto') && <><label>Horizontal framing <input type="range" min="-1" max="1" step="0.05" value={horizontal} onChange={e => setHorizontal(Number(e.target.value))} /></label><label>Vertical framing <input type="range" min="-1" max="1" step="0.05" value={vertical} onChange={e => setVertical(Number(e.target.value))} /></label><label>Zoom <input type="range" min="1" max="2" step="0.05" value={zoom} onChange={e => setZoom(Number(e.target.value))} /></label><button className="secondary-btn" onClick={() => {setHorizontal(0); setVertical(0); setZoom(1); chooseLayout('smart'); setTrack(true);}}>Reset to automatic tracking</button></>}
        {layout === 'smart' && <><label><input type="checkbox" checked={track} onChange={e => setTrack(e.target.checked)} /> Track faces</label><label>Detection interval (seconds) <input className="ingest-control" type="number" min="0.5" max="5" step="0.5" value={interval} onChange={e => setIntervalValue(Number(e.target.value))} /></label></>}
        {(layout === 'smart' || layout === 'auto') && <><label>Tracking engine <select className="ingest-control" value={engine} onChange={e => setEngine(e.target.value as typeof engine)}><option value="standard">Standard face framing</option><option value="subject">Subject tracking</option></select></label>{engine === 'subject' && <><label>Camera smoothness <input type="range" min="0.15" max="2" step="0.05" value={smoothTime} onChange={e => setSmoothTime(Number(e.target.value))} /></label><label><input type="checkbox" checked={speaker} onChange={e => setSpeaker(e.target.checked)} /> Follow active speaker (heuristic)</label></>}</>}
        {layout === 'auto' && <label><input type="checkbox" checked={stackTwo} onChange={e => setStackTwo(e.target.checked)} /> Stack two speakers when possible</label>}
        <label>Padding (seconds) <input className="ingest-control" type="number" min="0" max="1" step="0.05" value={pad} onChange={e => setPad(Number(e.target.value))} /></label><label><input type="checkbox" checked={normalizeAudio} onChange={e => setNormalizeAudio(e.target.checked)} /> Normalize audio loudness</label>
        <label><input type="checkbox" checked={captions && !noAudio} disabled={noAudio} onChange={e => setCaptions(e.target.checked)} /> Burn in captions</label>{captions && <><label><input type="checkbox" checked={forceCaptions} onChange={e => setForceCaptions(e.target.checked)} /> Add captions anyway</label><label>Caption style <select className="ingest-control" value={captionStyle} onChange={e => setCaptionStyle(e.target.value)}><option value="highlight">Highlight</option><option value="classic">Classic</option><option value="minimal">Minimal</option></select></label><label><input type="checkbox" checked={uppercase} onChange={e => setUppercase(e.target.checked)} /> Uppercase captions</label></>}
        {layout === 'split' && <label>Main portion <input type="range" min="0.5" max="0.8" step="0.05" value={ratio} onChange={e => setRatio(Number(e.target.value))} /></label>}
      </div></details>
      {clip && <ReviewPanel clip={clip} videoId={videoId} query={query} xOffset={xOffset} setXOffset={setXOffset} onUpdate={c=>{setClipOverride(c); onClipUpdated?.(c);}} />}
      <label>Start offset (seconds) <input type="number" min="-5" max="5" step=".1" value={startOffset} onChange={e=>setStartOffset(Number(e.target.value))}/></label><label>End offset (seconds) <input type="number" min="-5" max="5" step=".1" value={endOffset} onChange={e=>setEndOffset(Number(e.target.value))}/></label><label>Crop y offset <input type="range" min="-.35" max=".35" step=".01" value={yOffset} onChange={e=>setYOffset(Number(e.target.value))}/></label>
      <div className="render-previews"><div><strong>Crop preview</strong><p>First frame uses standard visual framing; subject tracking and captions appear in the final render.</p>{clip && previewQuery && !previewError ? <img className="crop-preview" alt="Representative crop preview" src={`/api/clips/${clip.id}/render-preview?${previewQuery}`} onError={() => setPreviewError(true)} /> : <p>Crop preview unavailable.</p>}</div><div><strong>Original video</strong><video ref={player} controls preload="metadata" src={`/api/videos/${videoId}/stream`} onTimeUpdate={() => { if (player.current && end.current !== null && player.current.currentTime >= end.current) { player.current.pause(); end.current = null; } }} /><button className="secondary-btn" onClick={() => { if (player.current && clip) { player.current.currentTime = clip.start; end.current = clip.end; void player.current.play().catch(() => setError('Unable to play source video in this browser.')); } }}>Play selected range</button></div></div>
      <div className="analysis-actions"><button className="primary-btn" disabled={busy || (layout==='letterbox' && !validLetterboxColor)} onClick={() => void start(false)}>Render selected clip</button><button className="secondary-btn" disabled={busy || !selected.length || (layout==='letterbox' && !validLetterboxColor)} onClick={() => void start(true)}>Render all selected clips in project</button></div>
    </>}
    {batchRows.length > 0 && <div className="job-progress"><strong>Latest batch: {batchRows.filter(r => r.status === 'completed').length}/{batchRows.length} completed</strong><progress max="100" value={batchProgress} /><span>{batchProgress}%</span></div>}
    <YouTubeUpload projectId={projectId} renders={thisVideo} />
    {thisVideo.map(r => <article className="clip-card" key={r.id}><strong>{r.clip_title}</strong>{r.video_id !== videoId && <p>From another video in this project</p>}<p>{r.stage} · {r.status}{r.error ? ` · ${r.error}` : ''}</p>{r.progress != null && <div className="job-progress"><progress max="100" value={r.progress} /><span>{Math.round(r.progress)}%</span></div>}{r.tracking_status && <p>Framing: {r.tracking_status.replace(/_/g, ' ')}</p>}{r.info?.speaker_status === 'no_transcript' && <p>No timed speech in this clip; used visual tracking instead.</p>}{r.info?.speaker_status === 'heuristic' && <p>Speaker heuristic: {r.info.tracks} tracks, {r.info.speaker_switches} switches. Review the result.</p>}{r.status === 'completed' && <>{r.info?.layout_used && <p>Layout used: {r.info.layout_used} · Removed: {r.info.removed_seconds ?? 0}s</p>}{r.info?.loudness && <p>Loudness: {r.info.loudness.before ?? 'unknown'} → {r.info.loudness.after ?? 'unknown'} LUFS</p>}<p>Checklist: {r.width===1080 && r.height===1920?'✓':'Check'} vertical 1080×1920 · {(r.info?.duration ?? 999)<=180?'✓':'Check'} within 180 seconds · {r.info?.loudness?.after != null && Math.abs(r.info.loudness.after-(r.settings?.loudness_lufs ?? -14))<=1.5?'✓':'Check'} audio normalized · {r.settings?.captions && r.settings.caption_safe_zone?'Review':'Not enabled'} caption safe zone</p><video controls preload="metadata" src={`/api/renders/${r.id}/stream`} /><a className="secondary-btn" href={`/api/renders/${r.id}/download`} onClick={notice}>Download MP4</a>{r.clip_id && <a className="secondary-btn" href={`/api/clips/${r.clip_id}/captions.srt${r.settings ? `?render_id=${encodeURIComponent(r.id)}` : ''}`} onClick={notice}>Download SRT</a>}{r.clip_id && <a className="secondary-btn" href={`/api/clips/${r.clip_id}/captions.vtt${r.settings ? `?render_id=${encodeURIComponent(r.id)}` : ''}`} onClick={notice}>Download VTT</a>}{r.clip_id && <MetadataCard clipId={r.clip_id}/>}</>}{active(r) && <button className="secondary-btn" disabled={busy} onClick={() => void action(`/api/renders/${r.id}/cancel`, 'POST')}>Cancel render</button>}{(r.status === 'failed' || r.status === 'cancelled') && <button className="secondary-btn" disabled={busy} onClick={() => void action(`/api/renders/${r.id}/retry`, 'POST')}>Retry render</button>}{!active(r) && <button className="delete-btn" disabled={busy} onClick={() => { if (window.confirm('Delete this render?')) void action(`/api/renders/${r.id}`, 'DELETE'); }}>Delete render</button>}</article>)}
  </section>;
}
