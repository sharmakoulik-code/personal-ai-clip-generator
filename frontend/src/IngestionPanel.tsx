import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react';
import { Link2, Upload, Trash2 } from 'lucide-react';

export type Project = { id: number; name: string; created_at: string };
type Job = { id: string; project_id: number; source: string; kind: string; status: string; progress: number | null; speed: number | null; error: string | null };
type Video = { id: string; title: string; source_type: string; duration: number | null; size_bytes: number; width: number; height: number; has_audio: boolean; has_thumbnail: boolean };
type Metadata = { url: string; video_id: string; title: string; thumbnail: string | null; channel: string | null; duration: number | null; formats: { format_id: string; height: number; ext: string; filesize: number | null }[] };
type Props = { projects: Project[]; selected: number | null; onSelect: (id: number | null) => void; online: boolean };
const active = new Set(['pending', 'retrieving metadata', 'uploading', 'downloading', 'validating']);
const duration = (n: number | null) => n == null ? 'Duration unavailable' : `${Math.floor(n / 60)}:${String(Math.floor(n % 60)).padStart(2, '0')}`;
const size = (n: number | null) => n == null ? 'Size unavailable' : `${(n / 1048576).toFixed(1)} MB`;

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(path, options);
  if (!res.ok) {
    let message = `Request failed (${res.status}).`;
    try { const body = await res.json(); if (typeof body.detail === 'string') message = body.detail; } catch { /* default */ }
    throw new Error(message);
  }
  return (res.status === 204 ? undefined : await res.json()) as T;
}
const json = (body: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

export default function IngestionPanel({ projects, selected, onSelect, online }: Props) {
  const [url, setUrl] = useState('');
  const [metadata, setMetadata] = useState<Metadata | null>(null);
  const [quality, setQuality] = useState('1080');
  const [jobs, setJobs] = useState<Job[]>([]);
  const [videos, setVideos] = useState<Video[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [uploadPercent, setUploadPercent] = useState<number | null>(null);
  const [uploadJob, setUploadJob] = useState<string | null>(null);
  const xhr = useRef<XMLHttpRequest | null>(null);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const [dragging, setDragging] = useState(false);
  const [chosenFile, setChosenFile] = useState<File | null>(null);
  const refresh = useCallback(async () => {
    if (!online) return;
    try {
      setJobs(await api<Job[]>('/api/jobs'));
      const nextVideos = selected ? await api<Video[]>(`/api/projects/${selected}/videos`) : [];
      if (selectedRef.current === selected) setVideos(nextVideos);
    } catch { setError('Unable to load imports. Check that the local backend is running.'); }
  }, [online, selected]);
  useEffect(() => { void refresh(); const timer = window.setInterval(() => { void refresh(); }, 2000); return () => window.clearInterval(timer); }, [refresh]);
  async function fetchMetadata() {
    setBusy(true); setError(''); setMetadata(null);
    try { setMetadata(await api<Metadata>('/api/videos/youtube/metadata', json({ url }))); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not retrieve video information.'); }
    finally { setBusy(false); }
  }
  async function startDownload() {
    if (!selected || !metadata) return;
    setBusy(true); setError('');
    try { await api('/api/videos/youtube/download', json({ project_id: selected, url: metadata.url, quality, title: metadata.title })); setMetadata(null); setUrl(''); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not start download.'); }
    finally { setBusy(false); }
  }
  async function upload(file: File) {
    if (!selected || uploadJob) return;
    setChosenFile(file); setError('');
    if (!/\.(mp4|mov|mkv|webm|avi)$/i.test(file.name) || !file.size) { setError('Choose a non-empty MP4, MOV, MKV, WEBM, or AVI file.'); return; }
    try {
      const job = await api<Job>('/api/videos/upload', json({ project_id: selected, filename: file.name, size: file.size }));
      setUploadJob(job.id); setUploadPercent(0); void refresh();
      const request = new XMLHttpRequest(); xhr.current = request;
      request.open('PUT', `/api/jobs/${job.id}/upload`);
      request.setRequestHeader('Content-Type', 'application/octet-stream');
      request.upload.onprogress = event => { if (event.lengthComputable) setUploadPercent(Math.min(100, event.loaded * 100 / event.total)); };
      request.onload = () => { if (request.status >= 400) { let detail = 'Upload failed. Check the job status.'; try { const response = JSON.parse(request.responseText); if (typeof response.detail === 'string') detail = response.detail; } catch { /* default */ } setError(detail); } xhr.current = null; setUploadJob(null); setUploadPercent(null); void refresh(); };
      request.onerror = () => { setError('Upload failed. Check your connection.'); xhr.current = null; setUploadJob(null); setUploadPercent(null); void refresh(); };
      request.onabort = () => { xhr.current = null; setUploadJob(null); setUploadPercent(null); void refresh(); };
      request.send(file);
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not start upload.'); }
  }
  async function cancel(job: Job) {
    setError('');
    try { await api(`/api/jobs/${job.id}/cancel`, { method: 'POST' }); if (uploadJob === job.id) xhr.current?.abort(); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not cancel job.'); }
  }
  async function remove(video: Video) {
    if (!window.confirm(`Delete “${video.title}” and its local video file? This cannot be undone.`)) return;
    try { await api(`/api/videos/${video.id}`, { method: 'DELETE' }); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not delete video.'); }
  }
  function drop(event: DragEvent<HTMLDivElement>) { event.preventDefault(); setDragging(false); const file = event.dataTransfer.files[0]; if (file) void upload(file); }
  return <section className="source-section" aria-labelledby="source-title">
    <div className="section-heading"><div><div className="section-kicker">YOUR SOURCE</div><h2 id="source-title">Bring your video</h2><p>Import videos you own or have permission to use.</p></div></div>
    <label className="project-select-label" htmlFor="ingestion-project">Project</label>
    <select id="ingestion-project" className="ingest-control" value={selected ?? ''} onChange={e => onSelect(e.target.value ? Number(e.target.value) : null)} disabled={!online}><option value="">Select a project</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
    {!projects.length && <p className="source-note">Create a project first, then select it here.</p>}
    {error && <div className="error ingest-error" role="alert"><span>{error}</span><button onClick={() => setError('')}>Dismiss</button></div>}
    <div className="source-grid"><div className="source-card"><div className="source-icon"><Link2 size={18} /></div><div className="source-body"><label htmlFor="youtube-url">YouTube URL</label><p>Paste a link, then review the video before downloading.</p>
      <input id="youtube-url" type="url" placeholder="https://youtube.com/watch?v=..." value={url} onChange={e => { setUrl(e.target.value); setMetadata(null); }} disabled={!online || busy} />
      <button className="secondary-btn ingest-button" disabled={!url.trim() || busy || !online} onClick={() => void fetchMetadata()}>{busy ? 'Working...' : 'Fetch information'}</button>
      {metadata && <div className="metadata-preview">{metadata.thumbnail && <img src={metadata.thumbnail} alt="Video thumbnail" referrerPolicy="no-referrer" />}<strong>{metadata.title}</strong><span>{metadata.channel || 'Unknown channel'} · {duration(metadata.duration)}</span><span>ID: {metadata.video_id} · {metadata.formats.length} video formats available</span><details><summary>Available formats</summary><ul>{metadata.formats.map((f, index) => <li key={`${f.format_id}-${index}`}>{f.height}p · {f.ext} · {size(f.filesize)}</li>)}</ul></details><span>{metadata.formats.some(f => f.filesize) ? `Estimated format sizes: ${metadata.formats.filter(f => f.filesize).slice(-3).map(f => `${f.height}p ${size(f.filesize)}`).join(', ')}` : 'Estimated size unavailable'}</span><label htmlFor="quality">Download quality</label><select id="quality" className="ingest-control" value={quality} onChange={e => setQuality(e.target.value)}><option value="720">720p</option><option value="1080">1080p</option><option value="best">Best available</option></select><button className="primary-btn ingest-button" disabled={!selected || busy} onClick={() => void startDownload()}>Confirm download</button>{!selected && <span>Select a project first.</span>}</div>}
    </div></div><div className={`source-card ${dragging ? 'dragging' : ''}`} onDragOver={e => { e.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={drop}><div className="source-icon"><Upload size={18} /></div><div className="source-body"><label htmlFor="video-upload">Upload a video</label><p>Drag and drop or choose an MP4, MOV, MKV, WEBM, or AVI file.</p><input id="video-upload" type="file" accept=".mp4,.mov,.mkv,.webm,.avi" disabled={!online || !selected || !!uploadJob} onChange={e => { const file = e.target.files?.[0]; if (file) void upload(file); e.target.value = ''; }} />{chosenFile && <p>Selected: {chosenFile.name} · {size(chosenFile.size)}</p>}{uploadPercent !== null && <div className="job-progress"><progress max="100" value={uploadPercent} /><span>Uploading {Math.round(uploadPercent)}%</span></div>}</div></div></div>
    <div className="ingest-subsection"><h3>Import jobs</h3>{jobs.length === 0 ? <p className="source-note">No imports yet.</p> : <div className="ingest-list">{jobs.map(job => <div className="ingest-row" key={job.id}><div className="ingest-row-main"><strong>{job.source}</strong><span>{job.status}{job.speed ? ` · ${size(job.speed)}/s` : ''}{job.error ? ` · ${job.error}` : ''}</span>{job.progress !== null && active.has(job.status) && <div className="job-progress"><progress max="100" value={job.progress} /><span>{Math.round(job.progress)}% {job.status === 'downloading' ? '(current stream)' : ''}</span></div>}</div>{active.has(job.status) && <button className="secondary-btn" onClick={() => void cancel(job)}>Cancel</button>}</div>)}</div>}</div>
    <div className="ingest-subsection"><h3>Project videos</h3>{!selected ? <p className="source-note">Select a project to view its videos.</p> : videos.length === 0 ? <p className="source-note">No videos imported into this project yet.</p> : <div className="ingest-list">{videos.map(video => <div className="ingest-row" key={video.id}>{video.has_thumbnail && <img className="video-thumb" src={`/api/videos/${video.id}/thumbnail`} alt="" />}<div className="ingest-row-main"><strong>{video.title}</strong><span>{video.source_type === 'youtube' ? 'YouTube' : 'Local upload'} · {duration(video.duration)} · {size(video.size_bytes)} · {video.width}×{video.height}{!video.has_audio ? ' · No audio' : ''}</span></div><button className="delete-btn" aria-label={`Delete video ${video.title}`} onClick={() => void remove(video)}><Trash2 size={17} /></button></div>)}</div>}</div>
  </section>;
}
