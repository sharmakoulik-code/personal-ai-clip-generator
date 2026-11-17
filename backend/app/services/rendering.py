"""Persistent single-process FFmpeg rendering queue."""
import hashlib
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from .. import database
from . import framing, ingestion, media

log = logging.getLogger(__name__)
_stop = threading.Event()
_worker = None

class Cancelled(Exception): pass


def get(render_id):
    with database.connect() as db:
        row = db.execute('SELECT * FROM renders WHERE id=?', (render_id,)).fetchone()
    return dict(row) if row else None


def public(row):
    if not row: return None
    return {k: v for k, v in row.items() if k not in ('path', 'thumbnail_path', 'settings_json', 'cancel_requested')} | {'settings': json.loads(row['settings_json'])}


def list_renders(video_id=None, project_id=None):
    with database.connect() as db:
        if video_id: rows = db.execute('SELECT * FROM renders WHERE video_id=? ORDER BY rowid DESC LIMIT 200', (video_id,)).fetchall()
        elif project_id is not None: rows = db.execute('SELECT * FROM renders WHERE project_id=? ORDER BY rowid DESC LIMIT 200', (project_id,)).fetchall()
        else: rows = db.execute('SELECT * FROM renders ORDER BY rowid DESC LIMIT 200').fetchall()
    return [public(dict(r)) for r in rows]


def queue_clips(clip_ids, settings):
    batch = uuid.uuid4().hex
    ids = []
    with database.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for clip_id in clip_ids:
            c = db.execute('SELECT c.*,v.project_id,v.duration FROM candidate_clips c JOIN videos v ON v.id=c.video_id WHERE c.id=?', (clip_id,)).fetchone()
            if not c: raise ValueError('Clip not found.')
            if c['start'] < 0 or c['end'] <= c['start'] or not c['duration'] or c['end'] > c['duration'] + .05: raise ValueError('Clip timestamps are outside the source video.')
            if db.execute("SELECT 1 FROM analysis_jobs WHERE video_id=? AND status IN ('pending','running')", (c['video_id'],)).fetchone(): raise ValueError('Wait for analysis to finish before rendering.')
            if db.execute("SELECT 1 FROM renders WHERE clip_id=? AND status IN ('pending','running')", (clip_id,)).fetchone(): raise ValueError('A render is already active for this clip.')
            rid = uuid.uuid4().hex; ids.append(rid)
            height = settings['resolution']; width = height * 9 // 16
            db.execute('''INSERT INTO renders(id,batch_id,project_id,video_id,clip_id,clip_title,start,end,layout,width,height,settings_json,status,stage)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'pending','Queued')''', (rid,batch,c['project_id'],c['video_id'],clip_id,c['title'],c['start'],c['end'],settings['layout'],width,height,json.dumps(settings)))
    return [public(get(i)) for i in ids]


def check_cancel(rid):
    row = get(rid)
    if _stop.is_set() or not row or row['cancel_requested']: raise Cancelled()


def update(rid, stage, progress=None):
    with database.connect() as db: db.execute("UPDATE renders SET stage=?,progress=? WHERE id=? AND status='running'", (stage,progress,rid))


def cancel(rid):
    with database.connect() as db:
        row = db.execute('SELECT status FROM renders WHERE id=?', (rid,)).fetchone()
        if not row: return False
        if row['status'] == 'pending': db.execute("UPDATE renders SET status='cancelled',stage='Cancelled',cancel_requested=1,completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (rid,))
        elif row['status'] == 'running': db.execute('UPDATE renders SET cancel_requested=1 WHERE id=?', (rid,))
    return True


def source_video(video_id):
    video = ingestion.get_video(video_id)
    if not video: raise ValueError('Source video not found.')
    path = Path(video['path']).resolve()
    if not path.is_relative_to(media.storage_root() / 'videos') or not path.is_file(): raise ValueError('Source video is missing or outside storage.')
    return video, path


def track(video, path, start, end, settings, check, progress):
    if settings['layout'] != 'smart' or not settings['track_faces']:
        return [{'t': 0, 'x': .5, 'y': .5}], 'center'
    key = hashlib.sha256(json.dumps([video['id'], path.stat().st_mtime_ns, path.stat().st_size, start, end, settings['detect_interval']], separators=(',', ':')).encode()).hexdigest()
    with database.connect() as db: cached = db.execute('SELECT data_json FROM face_track_cache WHERE key=?', (key,)).fetchone()
    if cached:
        data = json.loads(cached['data_json']); return data['points'], data['status']
    points, status = framing.detect_frames(path, start, end - start, settings['detect_interval'], check, progress)
    check()
    with database.connect() as db:
        db.execute('INSERT OR REPLACE INTO face_track_cache(key,video_id,data_json) VALUES(?,?,?)', (key, video['id'], json.dumps({'points': points, 'status': status})))
    return points, status


def filter_graph(video, settings, points, width, height):
    layout = settings['layout']
    if layout in ('smart', 'center'):
        cw,ch,pos = framing.crop_positions(points, video['width'], video['height'], settings['zoom'], settings['horizontal'], settings['vertical'])
        x = framing.expression(pos, 'x'); y = framing.expression(pos, 'y')
        return f"[0:v]setpts=PTS-STARTPTS,crop={cw}:{ch}:'{x}':'{y}',scale={width}:{height}:flags=lanczos,setsar=1[v]"
    if layout == 'blur':
        return f"[0:v]setpts=PTS-STARTPTS,split=2[bg][fg];[bg]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},boxblur=20:2[back];[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[front];[back][front]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
    top = int(height * settings['split_ratio']) // 2 * 2; bottom = height - top
    return f"[0:v]setpts=PTS-STARTPTS,split=2[main][secondary];[main]scale={width}:{top}:force_original_aspect_ratio=decrease,pad={width}:{top}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[a];[secondary]scale={width}:{bottom}:force_original_aspect_ratio=increase,crop={width}:{bottom},setsar=1[b];[a][b]vstack=inputs=2[v]"


def preview(clip, settings):
    video, path = source_video(clip['video_id'])
    if settings['layout'] == 'smart' and settings['track_faces']:
        points, status = framing.detect_frames(path, clip['start'], .1, 1.0)
    else: points, status = [{'t':0,'x':.5,'y':.5}], 'center'
    graph = filter_graph(media.probe_video(path), settings, points, 270, 480)
    args = [os.environ.get('CLIP_FFMPEG','ffmpeg'), '-hide_banner', '-loglevel','error','-ss',str(clip['start']),'-protocol_whitelist','file,pipe','-i',str(path),'-filter_complex',graph,'-map','[v]','-frames:v','1','-f','image2pipe','-c:v','mjpeg','pipe:1']
    try: result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as exc: raise ValueError('Could not create the crop preview.') from exc
    if not result.stdout: raise ValueError('Could not create the crop preview.')
    return result.stdout, status


def run_ffmpeg(args, rid, duration):
    try: proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except OSError as exc: raise ValueError('FFmpeg could not start. Verify it is installed.') from exc
    lines = queue.Queue()
    def reader():
        try:
            for line in proc.stdout: lines.put(line)
        finally: proc.stdout.close()
    threading.Thread(target=reader, daemon=True).start()
    try:
        while proc.poll() is None or not lines.empty():
            check_cancel(rid)
            try: line = lines.get(timeout=.2).strip()
            except queue.Empty: continue
            if line.startswith('out_time_ms='):
                try: update(rid, 'Rendering video', 20 + min(1, max(0, int(line.split('=',1)[1]) / 1_000_000 / duration)) * 75)
                except ValueError: pass
        if proc.wait() != 0: raise ValueError('FFmpeg could not render this video. Check codec support and disk space.')
    finally:
        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()


def encoder_args(quality):
    """Explicit opt-in hardware encoding; CPU is the portable default."""
    cq = {'high': '18', 'balanced': '22', 'small': '26'}[quality]
    encoder = os.environ.get('CLIP_RENDER_ENCODER', 'cpu').lower()
    if encoder == 'cpu': return ['-c:v', 'libx264', '-preset', 'fast', '-crf', cq]
    if encoder == 'nvenc': return ['-c:v', 'h264_nvenc', '-preset', 'p4', '-cq', cq, '-b:v', '0']
    raise ValueError('CLIP_RENDER_ENCODER must be cpu or nvenc.')


def run_job(job):
    rid = job['id']; temp = media.storage_path('temp', job['project_id'], 'render-' + rid)
    target = media.storage_path('rendered', job['project_id'], rid + '.mp4')
    thumb = media.storage_path('rendered', job['project_id'], rid + '.jpg')
    try:
        check_cancel(rid); video, path = source_video(job['video_id']); settings = json.loads(job['settings_json'])
        details = media.probe_video(path)
        temp.mkdir(parents=True, exist_ok=True); target.parent.mkdir(parents=True, exist_ok=True)
        update(rid, 'Detecting faces', 0)
        points, status = track(video, path, job['start'], job['end'], settings, lambda: check_cancel(rid), lambda p: update(rid, 'Detecting faces', p * 20))
        check_cancel(rid); update(rid, 'Rendering video', 20)
        output = temp / 'output.mp4'; graph = filter_graph(details, settings, points, job['width'], job['height'])
        codec = encoder_args(settings['quality'])
        args = [os.environ.get('CLIP_FFMPEG','ffmpeg'),'-hide_banner','-loglevel','error','-nostdin','-y','-ss',str(job['start']),'-protocol_whitelist','file,pipe','-i',str(path),'-t',str(job['end']-job['start']),'-filter_complex',graph,'-map','[v]','-map','0:a:0?',*codec,'-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart','-progress','pipe:1','-nostats',str(output)]
        run_ffmpeg(args, rid, job['end']-job['start']); check_cancel(rid)
        probe = media.probe_video(output)
        if (probe['width'] != job['width'] or probe['height'] != job['height']
                or probe['video_codec'] != 'h264' or (video['has_audio'] and probe['audio_codec'] != 'aac')
                or not probe['duration'] or abs(probe['duration'] - (job['end'] - job['start'])) > max(.5, (job['end'] - job['start']) * .02)):
            raise ValueError('Rendered output failed validation (dimensions, codecs or duration).')
        temporary_thumb = temp / 'thumbnail.jpg'
        has_thumb = media.make_thumbnail(output, temporary_thumb)
        check_cancel(rid)
        with database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT cancel_requested FROM renders WHERE id=?', (rid,)).fetchone()
            if not current or current['cancel_requested'] or _stop.is_set(): raise Cancelled()
            output.replace(target)
            if has_thumb: temporary_thumb.replace(thumb)
            db.execute("UPDATE renders SET status='completed',stage='Render completed',progress=100,path=?,thumbnail_path=?,tracking_status=?,completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (str(target), str(thumb) if has_thumb else None, status, rid))
    except Cancelled:
        target.unlink(missing_ok=True); thumb.unlink(missing_ok=True)
        with database.connect() as db: db.execute("UPDATE renders SET status='cancelled',stage='Cancelled',error='Render cancelled or interrupted.',completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (rid,))
    except Exception as exc:
        target.unlink(missing_ok=True); thumb.unlink(missing_ok=True)
        log.exception('Render failed: %s', rid)
        message = str(exc) if isinstance(exc, ValueError) else 'Render failed. Check local resources and video format.'
        with database.connect() as db: db.execute("UPDATE renders SET status='failed',stage='Failed',error=?,completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (message,rid))
    finally: shutil.rmtree(temp, ignore_errors=True)


def loop():
    while not _stop.wait(.25):
        with database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM renders WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
            if row: db.execute("UPDATE renders SET status='running',stage='Preparing render' WHERE id=?", (row['id'],))
        if row: run_job(dict(row))


def start_worker():
    global _worker
    if _worker and _worker.is_alive(): raise RuntimeError('Previous render worker is still stopping.')
    _stop.clear(); _worker = None
    with database.connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM renders WHERE status='running'")]
        db.execute("UPDATE renders SET status='failed',stage='Interrupted',error='Render interrupted; retry it.',completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE status='running'")
    for row in rows:
        shutil.rmtree(media.storage_path('temp',row['project_id'],'render-'+row['id']), ignore_errors=True)
        media.storage_path('rendered',row['project_id'],row['id']+'.mp4').unlink(missing_ok=True)
        media.storage_path('rendered',row['project_id'],row['id']+'.jpg').unlink(missing_ok=True)
    if os.environ.get('CLIP_DISABLE_WORKER') != '1':
        _worker = threading.Thread(target=loop, name='render-worker', daemon=True); _worker.start()


def stop_worker():
    _stop.set()
    if _worker: _worker.join(timeout=5)
