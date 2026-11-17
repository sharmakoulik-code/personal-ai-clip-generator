"""Persistent SQLite queue and a single local ingestion worker."""
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from yt_dlp import YoutubeDL

from .. import database
from . import media

ACTIVE = {'pending', 'uploading', 'downloading', 'validating', 'retrieving metadata'}
TERMINAL = {'completed', 'failed', 'cancelled'}
_stop = threading.Event()
_worker: threading.Thread | None = None


class Cancelled(Exception):
    pass


def get_job(job_id: str) -> dict | None:
    with database.connect() as db:
        row = db.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs() -> list[dict]:
    with database.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT 200')]


def project_exists(project_id: int) -> bool:
    with database.connect() as db:
        return bool(db.execute('SELECT 1 FROM projects WHERE id=?', (project_id,)).fetchone())


def create_job(project_id: int, kind: str, source: str, quality: str | None = None, title: str | None = None) -> dict:
    job_id = uuid.uuid4().hex
    with database.connect() as db:
        db.execute('INSERT INTO jobs (id,project_id,kind,source,quality,title,status) VALUES (?,?,?,?,?,?,?)',
                   (job_id, project_id, kind, source, quality, title, 'uploading' if kind == 'upload' else 'pending'))
    return get_job(job_id)


def update_job(job_id: str, **fields) -> None:
    allowed = {'status', 'progress', 'speed', 'error', 'temp_path', 'video_id', 'cancel_requested', 'completed_at'}
    if not fields or not set(fields) <= allowed:
        raise ValueError('Invalid job update')
    with database.connect() as db:
        db.execute('UPDATE jobs SET ' + ', '.join(k + ' = ?' for k in fields) + ' WHERE id = ?', (*fields.values(), job_id))


def finish(job_id: str, status: str, error: str | None = None, video_id: str | None = None) -> None:
    with database.connect() as db:
        db.execute("UPDATE jobs SET status=?, error=?, video_id=?, speed=NULL, completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), progress=CASE WHEN ?='completed' THEN 100 ELSE progress END WHERE id=?",
                   (status, error, video_id, status, job_id))


def check_cancel(job_id: str) -> None:
    job = get_job(job_id)
    if job is None or job['cancel_requested'] or _stop.is_set():
        raise Cancelled()


def cancel_job(job_id: str) -> bool:
    cleanup = None
    with database.connect() as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job:
            return False
        if job['status'] not in TERMINAL:
            db.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (job_id,))
            if job['status'] == 'pending' or (job['status'] == 'uploading' and not job['temp_path']):
                db.execute("UPDATE jobs SET status='cancelled', completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (job_id,))
                cleanup = job['temp_path']
    if cleanup:
        path = Path(cleanup).resolve()
        if path.is_relative_to(media.storage_root() / 'temp'):
            path.unlink(missing_ok=True)
    return True


def list_videos(project_id: int) -> list[dict]:
    with database.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM videos WHERE project_id=? ORDER BY created_at DESC', (project_id,))]


def get_video(video_id: str) -> dict | None:
    with database.connect() as db:
        row = db.execute('SELECT * FROM videos WHERE id=?', (video_id,)).fetchone()
    return dict(row) if row else None


def delete_video(video_id: str) -> bool:
    with database.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM videos WHERE id=?', (video_id,)).fetchone()
        if not row:
            return False
        video = dict(row)
        if db.execute("SELECT 1 FROM analysis_jobs WHERE video_id=? AND status IN ('pending','running')", (video_id,)).fetchone():
            raise RuntimeError('Cancel the active analysis before deleting this video.')
        for key in ('path', 'thumbnail_path'):
            if video[key]:
                path = Path(video[key]).resolve()
                if not path.is_relative_to(media.storage_root()):
                    raise ValueError('Invalid stored video path')
                path.unlink(missing_ok=True)
        db.execute('DELETE FROM videos WHERE id=?', (video_id,))
    return True


def save_video(job: dict, temporary: Path, source_id: str | None = None) -> str:
    check_cancel(job['id'])
    update_job(job['id'], status='validating', progress=None, speed=None)
    details = media.probe_video(temporary)
    check_cancel(job['id'])
    video_id = job['id']
    dest = media.storage_path('videos', job['project_id'], video_id + temporary.suffix.lower())
    thumbnail = media.storage_path('thumbnails', job['project_id'], video_id + '.jpg')
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(temporary), str(dest))
    try:
        has_thumbnail = media.make_thumbnail(dest, thumbnail)
        check_cancel(job['id'])
        with database.connect() as db:
            current = db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job['id'],)).fetchone()
            if not current or current['cancel_requested']:
                raise Cancelled()
            db.execute('''INSERT INTO videos (id,project_id,source_type,source_id,title,path,thumbnail_path,duration,width,height,frame_rate,video_codec,audio_codec,has_audio,size_bytes)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (video_id, job['project_id'], job['kind'], source_id, job['title'] or job['source'], str(dest), str(thumbnail) if has_thumbnail else None,
                        details['duration'], details['width'], details['height'], details['frame_rate'], details['video_codec'], details['audio_codec'], int(details['has_audio']), details['size_bytes']))
            db.execute("UPDATE jobs SET status='completed', video_id=?, progress=100, speed=NULL, completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (video_id, job['id']))
    except BaseException:
        dest.unlink(missing_ok=True)
        thumbnail.unlink(missing_ok=True)
        raise
    return video_id


def download(job: dict) -> None:
    canonical, source_id = media.canonical_youtube_url(job['source'])
    with database.connect() as db:
        existing = db.execute('SELECT id,path FROM videos WHERE project_id=? AND source_type=? AND source_id=? ORDER BY created_at DESC',
                              (job['project_id'], 'youtube', source_id)).fetchall()
    for video in existing:
        if Path(video['path']).is_file():
            check_cancel(job['id'])
            finish(job['id'], 'completed', video_id=video['id'])
            return
    tmp_dir = media.storage_path('temp', job['project_id'], job['id'])
    tmp_dir.mkdir(parents=True, exist_ok=True)
    update_job(job['id'], status='downloading', progress=None)
    def hook(progress):
        check_cancel(job['id'])
        if progress.get('status') == 'downloading':
            total = progress.get('total_bytes') or progress.get('total_bytes_estimate')
            pct = min(99.0, progress.get('downloaded_bytes', 0) * 100 / total) if total else None
            update_job(job['id'], progress=pct, speed=progress.get('speed'))
    # Prefer MP4 + M4A where available, then fall back to other containers.
    limit = '' if job['quality'] == 'best' else f"[height<={job['quality']}]"
    fmt = f'bestvideo[ext=mp4]{limit}+bestaudio[ext=m4a]/best{limit}/best'
    opts = {'format': fmt, 'merge_output_format': 'mp4', 'outtmpl': str(tmp_dir / 'source.%(ext)s'),
            'noplaylist': True, 'quiet': True, 'no_warnings': True, 'ignoreconfig': True,
            'socket_timeout': 20, 'retries': 0, 'fragment_retries': 0, 'extractor_retries': 0,
            'progress_hooks': [hook], 'ffmpeg_location': os.environ.get('CLIP_FFMPEG') or None}
    try:
        check_cancel(job['id'])
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(canonical, download=True)
        check_cancel(job['id'])
        files = [p for p in tmp_dir.iterdir() if p.is_file() and p.suffix.lower() in media.SUPPORTED_EXTENSIONS]
        if not info or len(files) != 1:
            raise ValueError('Download did not produce a usable video. Check FFmpeg and available formats.')
        save_video(job, files[0], source_id)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run_job(job: dict) -> None:
    try:
        check_cancel(job['id'])
        if job['kind'] == 'youtube':
            download(job)
        else:
            path = Path(job['temp_path'] or '').resolve()
            expected = media.storage_path('temp', job['project_id'], job['id'] + Path(job['source']).suffix.lower())
            if path != expected or not path.is_file():
                raise ValueError('Uploaded file is missing; please upload again.')
            try:
                save_video(job, path)
            finally:
                path.unlink(missing_ok=True)
    except Cancelled:
        finish(job['id'], 'cancelled', 'Cancelled or interrupted.')
    except ValueError as exc:
        finish(job['id'], 'failed', str(exc))
    except Exception:
        # A blocked download may fail before the next progress callback sees cancellation.
        current = get_job(job['id'])
        if current and current['cancel_requested']:
            finish(job['id'], 'cancelled', 'Cancelled.')
        else:
            # Do not leak local paths or third-party exception internals to the browser.
            import logging
            logging.getLogger(__name__).exception('Ingestion failed for job %s', job['id'])
            finish(job['id'], 'failed', 'Import failed. Check the file, FFmpeg installation, permissions, and video availability.')


def _loop() -> None:
    while not _stop.wait(0.25):
        with database.connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY created_at, rowid LIMIT 1").fetchone()
            if job:
                db.execute("UPDATE jobs SET status='retrieving metadata' WHERE id=? AND status='pending'", (job['id'],))
        if job:
            run_job(dict(job))


def start_worker() -> None:
    global _worker
    _stop.clear()
    # Incomplete transfers cannot be safely resumed. Keep their records and remove partial files.
    with database.connect() as db:
        interrupted = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE status IN ('uploading','downloading','validating','retrieving metadata')")]
        db.execute("UPDATE jobs SET status='failed', error='Interrupted by application shutdown; please retry.', completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE status IN ('uploading','downloading','validating','retrieving metadata')")
    for job in interrupted:
        # A crash between moving a file and committing its SQLite record can leave
        # an orphan named after this job; remove it only if no video record exists.
        if not get_video(job['id']):
            video_dir = media.storage_path('videos', job['project_id'])
            if video_dir.exists():
                for orphan in video_dir.glob(job['id'] + '.*'):
                    orphan.unlink(missing_ok=True)
            media.storage_path('thumbnails', job['project_id'], job['id'] + '.jpg').unlink(missing_ok=True)
        shutil.rmtree(media.storage_path('temp', job['project_id'], job['id']), ignore_errors=True)
        if job['temp_path']:
            path = Path(job['temp_path']).resolve()
            if path.is_relative_to(media.storage_root() / 'temp'):
                path.unlink(missing_ok=True)
    if os.environ.get('CLIP_DISABLE_WORKER') != '1':
        _worker = threading.Thread(target=_loop, name='local-ingestion-worker', daemon=True)
        _worker.start()


def stop_worker() -> None:
    _stop.set()
    if _worker:
        _worker.join(timeout=5)
