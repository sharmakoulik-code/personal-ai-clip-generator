"""Persistent, local, single-worker transcription and clip-analysis queue."""
import json
import logging
import math
import os
import shutil
import sqlite3
import subprocess
import threading
import uuid
from pathlib import Path

from .. import database
from . import clip_selection, gemini_enhancement, groq_enhancement, ingestion, media

logger = logging.getLogger(__name__)
_stop = threading.Event()
_thread: threading.Thread | None = None
_model = None
_model_key = None
ACTIVE = ('pending', 'running')


class Cancelled(Exception):
    pass


def job_detail(job_id: str) -> dict | None:
    with database.connect() as db:
        row = db.execute('SELECT * FROM analysis_jobs WHERE id=?', (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(video_id: str) -> list[dict]:
    with database.connect() as db:
        return [dict(row) for row in db.execute('SELECT * FROM analysis_jobs WHERE video_id=? ORDER BY rowid DESC LIMIT 30', (video_id,))]


def queue_job(video: dict, model: str, minimum: int, maximum: int, force: bool, groq_titles: bool = False, gemini_titles: bool = False) -> dict:
    if groq_titles and not os.environ.get('GROQ_API_KEY', '').strip():
        raise ValueError('GROQ_API_KEY is not configured in the backend environment.')
    if gemini_titles and not os.environ.get('GEMINI_API_KEY', '').strip():
        raise ValueError('GEMINI_API_KEY is not configured in the backend environment.')
    job_id = uuid.uuid4().hex
    try:
        with database.connect() as db:
            db.execute('''INSERT INTO analysis_jobs (id,video_id,project_id,model,min_duration,max_duration,force_transcribe,groq_titles,gemini_titles,status,stage)
                          VALUES (?,?,?,?,?,?,?,?,?,'pending','Queued')''', (job_id, video['id'], video['project_id'], model, minimum, maximum, int(force), int(groq_titles), int(gemini_titles)))
    except sqlite3.IntegrityError as exc:
        raise ValueError('An analysis is already active for this video.') from exc
    return job_detail(job_id)


def update(job_id: str, *, stage: str, progress: float | None = None) -> None:
    with database.connect() as db:
        db.execute("UPDATE analysis_jobs SET stage=?, progress=? WHERE id=? AND status='running'", (stage, progress, job_id))


def finish(job_id: str, status: str, error: str | None = None) -> None:
    with database.connect() as db:
        db.execute("UPDATE analysis_jobs SET status=?, stage=?, error=?, progress=NULL, completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (status, status.capitalize(), error, job_id))


def check_cancel(job_id: str) -> None:
    job = job_detail(job_id)
    if _stop.is_set() or job is None or job['cancel_requested']:
        raise Cancelled()


def cancel(job_id: str) -> bool:
    with database.connect() as db:
        job = db.execute('SELECT status FROM analysis_jobs WHERE id=?', (job_id,)).fetchone()
        if not job: return False
        if job['status'] == 'pending':
            db.execute("UPDATE analysis_jobs SET cancel_requested=1,status='cancelled',stage='Cancelled',completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (job_id,))
        elif job['status'] == 'running':
            db.execute('UPDATE analysis_jobs SET cancel_requested=1 WHERE id=?', (job_id,))
    return True


def get_transcript(video_id: str) -> dict | None:
    with database.connect() as db:
        row = db.execute('SELECT * FROM transcripts WHERE video_id=?', (video_id,)).fetchone()
        if not row: return None
        segs = db.execute('SELECT * FROM transcript_segments WHERE video_id=? ORDER BY position', (video_id,)).fetchall()
    return dict(row, segments=[{'start': s['start'], 'end': s['end'], 'text': s['text'], 'words': json.loads(s['words_json'])} for s in segs])


def list_clips(video_id: str) -> list[dict]:
    with database.connect() as db:
        rows = db.execute('SELECT * FROM candidate_clips WHERE video_id=? ORDER BY score DESC, start', (video_id,)).fetchall()
    return [dict({k: row[k] for k in row.keys() if k != 'scores_json'}, scores=json.loads(row['scores_json']), selected=bool(row['selected'])) for row in rows]


def whisper_model(name: str):
    global _model, _model_key
    # Keep model-download caches under the configured storage directory.
    root = media.storage_root() / 'models'
    root.mkdir(parents=True, exist_ok=True)
    os.environ['HF_XET_CACHE'] = str(root / 'xet')
    from faster_whisper import WhisperModel
    import ctranslate2
    device = os.environ.get('CLIP_WHISPER_DEVICE', 'cpu').lower()
    if device == 'auto':
        try: device = 'cuda' if ctranslate2.get_cuda_device_count() > 0 else 'cpu'
        except Exception: device = 'cpu'
    if device not in ('cpu', 'cuda'):
        raise ValueError('CLIP_WHISPER_DEVICE must be cpu, cuda, or auto.')
    key = (name, device, str(root))
    if _model is None or _model_key != key:
        _model = None
        _model_key = None
        try:
            # An explicitly provisioned converted model can be used offline.
            local = root / ('whisper-' + name)
            model_source = str(local) if (local / 'model.bin').is_file() else name
            _model = WhisperModel(model_source, device=device, compute_type='float16' if device == 'cuda' else 'int8', download_root=str(root))
        except Exception as exc:
            logger.exception('Whisper model initialization failed')
            raise ValueError('Could not load the Whisper model. Check model download access, disk space, and GPU configuration.') from exc
        _model_key = key
    return _model


def extract_chunk(source: Path, target: Path, start: float, length: float, job_id: str) -> None:
    args = [os.environ.get('CLIP_FFMPEG', 'ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-ss', str(start), '-protocol_whitelist', 'file,pipe', '-i', str(source), '-t', str(length),
            '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(target)]
    try:
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise ValueError('FFmpeg could not start. Install it and verify it is on PATH.') from exc
    try:
        while process.poll() is None:
            check_cancel(job_id)
            if _stop.wait(.2): check_cancel(job_id)
        if process.returncode != 0:
            raise ValueError('FFmpeg could not extract audio from this video.')
    finally:
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired: process.kill(); process.wait()


def transcribe(video: dict, job: dict) -> tuple[str | None, list[dict]]:
    """Decode bounded 60-second windows with one second of context either side."""
    if not video['has_audio']:
        raise ValueError('This video has no audio stream to transcribe.')
    duration = video['duration']
    if not duration or not math.isfinite(duration) or duration <= 0:
        raise ValueError('Video duration is unavailable; cannot safely transcribe this file.')
    path = Path(video['path']).resolve()
    if not path.is_relative_to(media.storage_root() / 'videos') or not path.is_file():
        raise ValueError('The source video file is missing or outside local storage.')
    tmp = media.storage_path('temp', video['project_id'], 'analysis-' + job['id'])
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        check_cancel(job['id']); update(job['id'], stage='Loading Whisper model')
        model = whisper_model(job['model'])
        language = None
        segments = []
        for offset in range(0, math.ceil(duration), 60):
            check_cancel(job['id']); update(job['id'], stage='Preparing audio', progress=100 * offset / duration)
            window_start = max(0.0, offset - 1.0)
            window_end = min(duration, offset + 61.0)
            chunk = tmp / 'chunk.wav'
            extract_chunk(path, chunk, window_start, window_end - window_start, job['id'])
            check_cancel(job['id']); update(job['id'], stage='Transcribing video', progress=100 * offset / duration)
            items, info = model.transcribe(str(chunk), language=language, word_timestamps=True, vad_filter=True, beam_size=5)
            detected_language = info.language
            before_count = len(segments)
            for segment in items:
                check_cancel(job['id'])
                words = []
                for word in segment.words or []:
                    start = max(0.0, min(duration, window_start + word.start))
                    end = max(start, min(duration, window_start + word.end))
                    # Some hallucinated repetitions have no usable timing.
                    if end - start > .005 and offset <= (start + end) / 2 < min(duration, offset + 60):
                        words.append({'start': round(start, 3), 'end': round(end, 3), 'word': word.word})
                if words:
                    segments.append({'start': words[0]['start'], 'end': words[-1]['end'], 'text': ''.join(w['word'] for w in words).strip(), 'words': words})
                elif not segment.words:
                    start = max(0.0, min(duration, window_start + segment.start)); end = max(start, min(duration, window_start + segment.end))
                    if offset <= (start + end) / 2 < min(duration, offset + 60) and segment.text.strip():
                        segments.append({'start': round(start, 3), 'end': round(end, 3), 'text': segment.text.strip(), 'words': []})
                # Use measured completed media time, not a guessed time estimate.
                processed = max(offset, min(offset + 60, window_start + segment.end, duration))
                update(job['id'], stage='Transcribing video', progress=max(0.0, 100 * processed / duration))
            # A silent first window should not lock in a guessed language.
            if language is None and len(segments) > before_count:
                language = detected_language
            chunk.unlink(missing_ok=True)
        return language, segments
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def store_results(job: dict, language: str | None, segments: list[dict], candidates: list[dict], replace_transcript: bool, duration: float) -> None:
    check_cancel(job['id'])
    with database.connect() as db:
        # Lock before checking cancellation so a late cancellation cannot be overwritten.
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT cancel_requested FROM analysis_jobs WHERE id=?', (job['id'],)).fetchone()
        if not current or current['cancel_requested']: raise Cancelled()
        if replace_transcript:
            db.execute('DELETE FROM transcripts WHERE video_id=?', (job['video_id'],))
            db.execute('INSERT INTO transcripts (video_id,model,language,duration) VALUES (?,?,?,?)', (job['video_id'], job['model'], language, duration))
            db.executemany('INSERT INTO transcript_segments (video_id,position,start,end,text,words_json) VALUES (?,?,?,?,?,?)',
                           [(job['video_id'], index, s['start'], s['end'], s['text'], json.dumps(s['words'], ensure_ascii=False)) for index, s in enumerate(segments)])
        db.execute('DELETE FROM candidate_clips WHERE video_id=?', (job['video_id'],))
        db.executemany('''INSERT INTO candidate_clips (id,video_id,start,end,title,hook,transcript,reason,score,scores_json,selected)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                       [(c['id'], job['video_id'], c['start'], c['end'], c['title'], c['hook'], c['transcript'], c['reason'], c['score'], json.dumps(c['scores']), int(c['selected'])) for c in candidates])
        db.execute("UPDATE analysis_jobs SET status='completed', stage='Analysis completed', progress=100, completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (job['id'],))


def run_job(job: dict) -> None:
    try:
        check_cancel(job['id'])
        video = ingestion.get_video(job['video_id'])
        if not video: raise ValueError('The source video no longer exists.')
        cached = get_transcript(video['id'])
        replace = bool(job['force_transcribe'] or not cached or cached['model'] != job['model'])
        if replace:
            language, segments = transcribe(video, job)
        else:
            language, segments = cached['language'], cached['segments']
        check_cancel(job['id']); update(job['id'], stage='Analyzing transcript')
        if not segments:
            raise ValueError('No speech was detected. No transcript or clips were saved.')
        update(job['id'], stage='Detecting and scoring candidate clips')
        clips = clip_selection.generate_candidates(segments, job['min_duration'], job['max_duration'], lambda: check_cancel(job['id']))
        if job['groq_titles'] and clips:
            check_cancel(job['id']); update(job['id'], stage='Improving titles with Groq')
            clips = groq_enhancement.enhance(clips, lambda: check_cancel(job['id']))
        if job['gemini_titles'] and clips:
            check_cancel(job['id']); update(job['id'], stage='Improving titles with Google Gemini')
            clips = gemini_enhancement.enhance(clips, lambda: check_cancel(job['id']))
        store_results(job, language, segments, clips, replace, video['duration'])
    except Cancelled:
        finish(job['id'], 'cancelled', 'Analysis cancelled or interrupted.')
    except ValueError as exc:
        finish(job['id'], 'failed', str(exc))
    except Exception:
        logger.exception('Analysis failed for job %s', job['id'])
        finish(job['id'], 'failed', 'Analysis failed. Check the video, model, and local system resources.')


def _loop():
    while not _stop.wait(.25):
        with database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM analysis_jobs WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
            if row: db.execute("UPDATE analysis_jobs SET status='running',stage='Preparing audio' WHERE id=? AND status='pending'", (row['id'],))
        if row: run_job(dict(row))


def start_worker():
    global _thread
    if _thread and _thread.is_alive():
        raise RuntimeError('Previous analysis worker is still stopping; restart the application.')
    _thread = None
    _stop.clear()
    with database.connect() as db:
        interrupted = [dict(row) for row in db.execute("SELECT * FROM analysis_jobs WHERE status='running'")]
        db.execute("UPDATE analysis_jobs SET status='failed',stage='Interrupted',error='Analysis interrupted; run it again.',completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE status='running'")
    for job in interrupted:
        shutil.rmtree(media.storage_path('temp', job['project_id'], 'analysis-' + job['id']), ignore_errors=True)
    if os.environ.get('CLIP_DISABLE_WORKER') != '1':
        _thread = threading.Thread(target=_loop, name='analysis-worker', daemon=True)
        _thread.start()


def stop_worker():
    _stop.set()
    if _thread: _thread.join(timeout=5)
