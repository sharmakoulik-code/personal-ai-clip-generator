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
from . import analysis, captions, framing, ingestion, media, reframe

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
    return {k: v for k, v in row.items() if k not in ('path', 'thumbnail_path', 'settings_json', 'cancel_requested', 'info_json')} | {'settings': json.loads(row['settings_json']), 'info': json.loads(row.get('info_json') or 'null')}


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
            pad = settings.get('pad', 0)
            clip_start = max(0, c['start'] - pad)
            clip_end = min(c['duration'], c['end'] + pad)
            db.execute('''INSERT INTO renders(id,batch_id,project_id,video_id,clip_id,clip_title,start,end,layout,width,height,settings_json,status,stage)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'pending','Queued')''', (rid,batch,c['project_id'],c['video_id'],clip_id,c['title'],clip_start,clip_end,settings['layout'],width,height,json.dumps(settings)))
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


def track(video, path, start, end, settings, check, progress, with_info=False):
    if settings['layout'] != 'smart' or not settings['track_faces']:
        result = ([{'t': 0, 'x': .5, 'y': .5}], 'center', {})
        return result if with_info else result[:2]
    engine = settings.get('tracking_engine', 'standard')
    transcript = analysis.get_transcript(video['id']) if engine == 'subject' and settings.get('active_speaker') else None
    words = [w for segment in (transcript or {}).get('segments', []) for w in segment.get('words', [])]
    speech = [(a, b) for a, b in reframe.speech_intervals(words) if b > start and a < end]
    model = media.storage_root() / 'models' / 'face_detection_yunet.onnx'
    model_state = (model.stat().st_size, model.stat().st_mtime_ns) if engine == 'subject' and model.is_file() else None
    key = hashlib.sha256(json.dumps([video['id'], path.stat().st_mtime_ns, path.stat().st_size, start, end, settings['detect_interval'], engine, settings.get('smooth_time'), settings.get('dead_zone'), settings.get('zoom'), settings.get('active_speaker'), speech, model_state], separators=(',', ':')).encode()).hexdigest()
    with database.connect() as db: cached = db.execute('SELECT data_json FROM face_track_cache WHERE key=?', (key,)).fetchone()
    if cached:
        data = json.loads(cached['data_json']); result = (data['points'], data['status'], data.get('info', {}))
        return result if with_info else result[:2]
    if engine == 'subject':
        points, status, info = subject_track(path, start, end, settings, check, speech, with_info=True)
        progress(1)
    else:
        points, status = framing.detect_frames(path, start, end - start, settings['detect_interval'], check, progress)
        info = {}
    check()
    with database.connect() as db:
        db.execute('INSERT OR REPLACE INTO face_track_cache(key,video_id,data_json) VALUES(?,?,?)', (key, video['id'], json.dumps({'points': points, 'status': status, 'info': info})))
    result = (points, status, info)
    return result if with_info else result[:2]


def subject_track(path, start, end, settings, check, speech=None, with_info=False):
    opts = reframe.ReframeOptions(zoom=settings.get('zoom', 1), smooth_time=settings.get('smooth_time', .55), dead_zone=settings.get('dead_zone', .12), active_speaker=settings.get('active_speaker', False))
    detector = reframe.FaceDetector(media.storage_root() / 'models', allow_download=os.environ.get('CLIP_NO_MODEL_DOWNLOAD') != '1')
    check()
    samples, (w, h, fps) = reframe.analyze_subject(path, start, end, opts, detector, check, speech=speech)
    cw, ch, camera = reframe.build_camera_path(samples, w, h, fps, opts, int(round((end-start)*fps)))
    if not camera: raise ValueError('Could not read frames for subject tracking.')
    # Preserve both sides of each detected hard cut; interpolate smoothly elsewhere.
    stride = max(1, (len(camera)+89)//90)
    indices = set(range(0, len(camera), stride)) | {len(camera)-1}
    for sample in samples:
        if sample.cut:
            idx = min(len(camera)-1, max(0, round((sample.t-start)*fps)))
            indices.update((idx, max(0,idx-1)))
    chosen = [camera[i] for i in sorted(indices)]
    points = [{'t':round(t-start, 3), 'x':round((x+cw/2)/w, 5), 'y':round((y+ch/2)/h, 5)} for t,x,y in chosen]
    source = 'face' if any(s.faces for s in samples) else ('motion' if any(s.source == 'motion' for s in samples) else 'center')
    ids = {f[5] for s in samples for f in s.faces if f[5] is not None}
    chosen = [s.speaker_id for s in samples if s.speaker_id is not None]
    info = {'detector': detector.kind, 'tracks': len(ids), 'speaker_switches': sum(a != b for a,b in zip(chosen,chosen[1:])), 'speaker_status': ('heuristic' if speech and opts.active_speaker else 'no_transcript' if opts.active_speaker else 'disabled')}
    result = (points, f'{detector.kind}_{source}_framing', info)
    return result if with_info else result[:2]


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


def run_ffmpeg(args, rid, duration, cwd=None):
    try: proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=cwd)
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
        points, status, info = track(video, path, job['start'], job['end'], settings, lambda: check_cancel(rid), lambda p: update(rid, 'Detecting faces', p * 20), with_info=True)
        check_cancel(rid); update(rid, 'Rendering video', 20)
        output = temp / 'output.mp4'; graph = filter_graph(details, settings, points, job['width'], job['height'])
        codec = encoder_args(settings['quality'])
        if settings.get('captions'):
            transcript = analysis.get_transcript(job['video_id']) or {}
            words = [w for segment in transcript.get('segments', []) for w in segment.get('words', [])]
            content = captions.build_ass(words, job['start'], job['end'], job['width'], job['height'], settings.get('caption_style', 'highlight'), settings.get('caption_uppercase', False))
            (temp / 'captions.ass').write_text(content, encoding='utf-8')
            font_option = ''
            fonts = os.environ.get('CLIP_CAPTION_FONTS_DIR')
            if fonts:
                font_dir = Path(fonts).expanduser().resolve()
                if not font_dir.is_dir(): raise ValueError('Caption font directory does not exist.')
                files = [p for p in font_dir.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() in ('.ttf','.otf')]
                if len(files)>100 or sum(p.stat().st_size for p in files)>100_000_000: raise ValueError('Caption font directory is too large.')
                local_fonts = temp / 'fonts'; local_fonts.mkdir()
                for font in files: shutil.copy2(font, local_fonts / font.name)
                font_option = ':fontsdir=fonts'
            graph = graph.removesuffix('[v]') + '[base];[base]ass=filename=captions.ass' + font_option + '[v]'
        graph_path = temp / 'filter.ffscript'
        graph_path.write_text(graph, encoding='utf-8')
        args = [os.environ.get('CLIP_FFMPEG','ffmpeg'),'-hide_banner','-loglevel','error','-nostdin','-y','-ss',str(job['start']),'-protocol_whitelist','file,pipe','-i',str(path),'-t',str(job['end']-job['start']),'-filter_complex_script',str(graph_path),'-map','[v]','-map','0:a:0?',*codec,'-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart','-progress','pipe:1','-nostats',str(output)]
        if settings.get('normalize_audio') and video['has_audio']:
            args[-1:-1] = ['-af', 'loudnorm=I=-16:TP=-1.5:LRA=11']
        if settings.get('captions'):
            executable = shutil.which(args[0])
            if not executable: raise ValueError('FFmpeg could not start. Verify it is installed.')
            args[0] = str(Path(executable).resolve())
            run_ffmpeg(args, rid, job['end']-job['start'], cwd=temp)
        else:
            run_ffmpeg(args, rid, job['end']-job['start'])
        check_cancel(rid)
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
            db.execute("UPDATE renders SET status='completed',stage='Render completed',progress=100,path=?,thumbnail_path=?,tracking_status=?,info_json=?,completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (str(target), str(thumb) if has_thumb else None, status, json.dumps(info), rid))
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
