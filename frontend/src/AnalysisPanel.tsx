import { useCallback, useEffect, useRef, useState } from 'react';

type Job = { id: string; status: string; stage: string; progress: number | null; error: string | null; model: string };
type Clip = { id: string; start: number; end: number; title: string; hook: string; transcript: string; reason: string; score: number; scores: Record<string, number>; selected: boolean };
type Segment = { start: number; end: number; text: string };
type Transcript = { language: string | null; model: string; segments: Segment[] };
type Props = { videoId: string; hasAudio: boolean };
const active = (job: Job | null) => !!job && (job.status === 'pending' || job.status === 'running');
const time = (seconds: number) => {
  const total = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
};

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try { const data = await response.json(); if (typeof data.detail === 'string') message = data.detail; } catch { /* default */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}
const json = (method: string, body: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

export default function AnalysisPanel({ videoId, hasAudio }: Props) {
  const [model, setModel] = useState('base');
  const [range, setRange] = useState('30-45');
  const [force, setForce] = useState(false);
  const [groqTitles, setGroqTitles] = useState(false);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [clips, setClips] = useState<Clip[]>([]);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [sort, setSort] = useState<'score' | 'duration'>('score');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const loadedResult = useRef<string | null>(null);
  const job = jobs[0] ?? null;

  const refresh = useCallback(async () => {
    try {
      const nextJobs = await api<Job[]>(`/api/videos/${videoId}/analysis-jobs`);
      setJobs(nextJobs);
      const newest = nextJobs[0];
      const key = newest ? `${newest.id}:${newest.status}` : 'none';
      // Avoid repeatedly transferring a potentially long transcript every poll.
      if (loadedResult.current !== null && (active(newest ?? null) || loadedResult.current === key)) return;
      const [clipsResult, transcriptionResult] = await Promise.all([
        fetch(`/api/videos/${videoId}/clips`), fetch(`/api/videos/${videoId}/transcription`),
      ]);
      if (!clipsResult.ok || (!transcriptionResult.ok && transcriptionResult.status !== 404)) throw new Error('Could not load analysis data.');
      setClips(await clipsResult.json());
      setTranscript(transcriptionResult.ok ? await transcriptionResult.json() : null);
      loadedResult.current = key;
    } catch { setError('Could not load analysis data. Check the backend connection.'); }
  }, [videoId]);
  useEffect(() => { void refresh(); const timer = window.setInterval(() => { void refresh(); }, 2500); return () => window.clearInterval(timer); }, [refresh]);

  async function start() {
    setBusy(true); setError('');
    try { await api(`/api/videos/${videoId}/analyze`, json('POST', { model, duration_range: range, force_transcribe: force, groq_titles: groqTitles })); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not start analysis.'); }
    finally { setBusy(false); }
  }
  async function cancel() {
    if (!job) return;
    setBusy(true); setError('');
    try { await api(`/api/analysis/jobs/${job.id}/cancel`, { method: 'POST' }); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not cancel.'); }
    finally { setBusy(false); }
  }
  async function select(clip: Clip, selected: boolean) {
    setBusy(true); setError('');
    try { const saved = await api<Clip>(`/api/clips/${clip.id}`, json('PATCH', { selected })); setClips(current => current.map(c => c.id === saved.id ? saved : c)); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not save selection.'); }
    finally { setBusy(false); }
  }
  async function selectAll(selected: boolean) {
    setBusy(true); setError('');
    try { setClips(await api<Clip[]>(`/api/videos/${videoId}/clips/selection`, json('PATCH', { selected }))); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not save selection.'); }
    finally { setBusy(false); }
  }
  const sorted = [...clips].sort((a, b) => sort === 'score' ? b.score - a.score : (b.end - b.start) - (a.end - a.start));

  return <div className="analysis-panel">
    <div className="analysis-heading"><div><h3>AI analysis</h3><p>Local Whisper transcription and explainable transcript-based suggestions.</p></div></div>
    {!hasAudio && <p className="analysis-notice">This video has no audio stream to transcribe.</p>}
    <div className="analysis-options"><label>Whisper model<select className="ingest-control" value={model} onChange={e => setModel(e.target.value)} disabled={busy || active(job)}><option value="tiny">Tiny (fastest)</option><option value="base">Base (default)</option><option value="small">Small</option><option value="medium">Medium</option></select></label><label>Clip duration<select className="ingest-control" value={range} onChange={e => setRange(e.target.value)} disabled={busy || active(job)}><option value="15-30">15–30 seconds</option><option value="30-45">30–45 seconds</option><option value="45-60">45–60 seconds</option><option value="60-90">60–90 seconds</option></select></label><label className="force-checkbox"><input type="checkbox" checked={force} onChange={e => setForce(e.target.checked)} disabled={busy || active(job)} /> Re-transcribe even if cached</label></div>
    <label className="force-checkbox"><input type="checkbox" checked={groqTitles} onChange={e => setGroqTitles(e.target.checked)} disabled={busy || active(job)} /> Improve titles and explanations with Groq (sends candidate transcript text to Groq)</label>
    <p className="analysis-notice">The selected model downloads on first use. Re-analysis replaces previous clip suggestions and selections only after success.</p>
    {error && <div className="error" role="alert"><span>{error}</span><button onClick={() => setError('')}>Dismiss</button></div>}
    <button className="primary-btn" onClick={() => void start()} disabled={!hasAudio || busy || active(job)}>{transcript ? 'Run analysis again' : 'Analyze Video with AI'}</button>
    {job && <div className="analysis-job"><strong>{job.stage}</strong><span>{job.status}{job.error ? ` · ${job.error}` : ''}</span>{job.progress != null && <div className="job-progress"><progress max="100" value={job.progress} /><span>{Math.round(job.progress)}% of source audio</span></div>}{active(job) && <button className="secondary-btn" disabled={busy} onClick={() => void cancel()}>Cancel analysis</button>}</div>}
    {transcript && <details className="analysis-transcript"><summary>View full transcript · {transcript.language || 'Language unknown'} · {transcript.model}</summary>{transcript.segments.map((seg, index) => <p key={index}><span>{time(seg.start)}–{time(seg.end)}</span> {seg.text}</p>)}</details>}
    {transcript && clips.length === 0 && !active(job) && <p className="analysis-notice">No suitable clips were found for this duration. Try another duration range.</p>}
    {clips.length > 0 && <div className="analysis-results"><div className="analysis-results-heading"><h3>Suggested clips ({clips.length})</h3><label>Sort by <select className="ingest-control" value={sort} onChange={e => setSort(e.target.value as 'score' | 'duration')}><option value="score">AI score</option><option value="duration">Duration</option></select></label></div><p className="analysis-notice">Scores are local heuristics, not predictions of virality. Hooks are transcript excerpts. Groq-generated titles and explanations, when enabled, can be inaccurate; review them.</p><div className="analysis-actions"><button className="secondary-btn" disabled={busy || active(job)} onClick={() => void selectAll(true)}>Select all</button><button className="secondary-btn" disabled={busy || active(job)} onClick={() => void selectAll(false)}>Deselect all</button></div>{sorted.map((clip, index) => <article className="clip-card" key={clip.id}><div className="clip-top"><label><input aria-label={`Select clip ${index + 1}`} type="checkbox" checked={clip.selected} disabled={busy || active(job)} onChange={e => void select(clip, e.target.checked)} /> <strong>Clip {String(index + 1).padStart(2, '0')} · {clip.title}</strong></label><span className="clip-score">{clip.score}/100</span></div><p>{time(clip.start)} – {time(clip.end)} · {Math.round(clip.end - clip.start)} seconds</p>{clip.reason.startsWith('Groq suggestion (review):') && <p>Title suggested by Groq · review accuracy</p>}<p><strong>Hook:</strong> {clip.hook}</p><p><strong>Why:</strong> {clip.reason}</p><details><summary>View clip transcript and scores</summary><p>{clip.transcript}</p><ul>{Object.entries(clip.scores).map(([key, value]) => <li key={key}>{key.replace(/_/g, ' ')}: {value}/100</li>)}</ul></details></article>)}</div>}
  </div>;
}
