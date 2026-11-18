"""Persistent single-process FFmpeg rendering queue."""
import hashlib
import json
import logging
import math
import re
import time
import os
import queue
import shutil
import subprocess
import threading
import uuid
from collections import OrderedDict

import numpy as np
from pathlib import Path

from .. import database
from . import analysis, captions, framing, ingestion, media, reframe, shorts, tighten

log = logging.getLogger(__name__)
_stop = threading.Event()
_worker = None
_preview_paths = OrderedDict()
_preview_lock = threading.Lock()

class Cancelled(Exception): pass


def get(render_id):
    with database.connect() as db:
        row = db.execute('SELECT * FROM renders WHERE id=?', (render_id,)).fetchone()
    return dict(row) if row else None


def public(row):
    if not row: return None
    return {k: v for k, v in row.items() if k not in ('path', 'thumbnail_path', 'settings_json', 'cancel_requested', 'info_json', 'caption_words_json')} | {'settings': json.loads(row['settings_json']), 'info': json.loads(row.get('info_json') or 'null')}


def list_renders(video_id=None, project_id=None):
    with database.connect() as db:
        if video_id: rows = db.execute('SELECT * FROM renders WHERE video_id=? ORDER BY rowid DESC LIMIT 200', (video_id,)).fetchall()
        elif project_id is not None: rows = db.execute('SELECT * FROM renders WHERE project_id=? ORDER BY rowid DESC LIMIT 200', (project_id,)).fetchall()
        else: rows = db.execute('SELECT * FROM renders ORDER BY rowid DESC LIMIT 200').fetchall()
    return [public(dict(r)) for r in rows]


def apply_range(start,end,duration,settings,pad=0):
    a=max(0.,min(duration,start-pad+settings.get('start_offset',0)))
    b=max(0.,min(duration,end+pad+settings.get('end_offset',0)))
    if settings.get('start_offset',0) or settings.get('end_offset',0):
        if duration<3: raise ValueError('Source is shorter than the minimum 3-second clip.')
        if b-a<3:
            b=min(duration,a+3)
            a=max(0,b-3)
    if b<=a: raise ValueError('Invalid clip range.')
    return a,b


def crop_with_offsets(points,width,height,settings):
    cw,ch,pos=framing.crop_positions(points,width,height,settings['zoom'],settings['horizontal'],settings['vertical'])
    for p in pos:
        p['x']=round(framing.clamp(p['x']+settings.get('x_offset',0)*cw,0,width-cw),2)
        p['y']=round(framing.clamp(p['y']+settings.get('y_offset',0)*ch,0,height-ch),2)
    return cw,ch,pos


def queue_clips(clip_ids, settings):
    batch = uuid.uuid4().hex
    ids = []
    with database.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for clip_id in clip_ids:
            c = db.execute('SELECT c.*,v.project_id,v.duration,v.preflight_json,v.has_audio FROM candidate_clips c JOIN videos v ON v.id=c.video_id WHERE c.id=?', (clip_id,)).fetchone()
            if not c: raise ValueError('Clip not found.')
            if settings.get('captions'):
                if not c['has_audio']: raise ValueError('Captions require source audio.')
                try: preflight_data=json.loads(c['preflight_json'] or '{}')
                except (ValueError,TypeError): preflight_data={}
                if not settings.get('force_captions') and any(isinstance(w,dict) and w.get('code')=='possible_burned_in_captions' for w in preflight_data.get('warnings',[])):
                    raise ValueError('Possible burned-in captions detected. Choose Add captions anyway to override.')
            if c['start'] < 0 or c['end'] <= c['start'] or not c['duration'] or c['end'] > c['duration'] + .05: raise ValueError('Clip timestamps are outside the source video.')
            if db.execute("SELECT 1 FROM analysis_jobs WHERE video_id=? AND status IN ('pending','running')", (c['video_id'],)).fetchone(): raise ValueError('Wait for analysis to finish before rendering.')
            if db.execute("SELECT 1 FROM renders WHERE clip_id=? AND status IN ('pending','running')", (clip_id,)).fetchone(): raise ValueError('A render is already active for this clip.')
            rid = uuid.uuid4().hex; ids.append(rid)
            height = shorts.RECOMMENDED_HEIGHT if settings.get('preset') == 'shorts' else settings['resolution']; width = height * 9 // 16
            pad = settings.get('pad', 0)
            clip_start, clip_end = apply_range(c['start'],c['end'],c['duration'],settings,pad)
            stored_settings = dict(settings, _source_clip_start=c['start'], _source_clip_end=c['end'])
            if settings.get('preset') == 'shorts' and clip_end - clip_start > shorts.MAX_DURATION:
                raise ValueError('Shorts cannot exceed 180 seconds.')
            db.execute('''INSERT INTO renders(id,batch_id,project_id,video_id,clip_id,clip_title,start,end,layout,width,height,settings_json,status,stage)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'pending','Queued')''', (rid,batch,c['project_id'],c['video_id'],clip_id,c['title'],clip_start,clip_end,settings['layout'],width,height,json.dumps(stored_settings)))
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


def shorts_track(video, path, start, end, settings, check, progress):
    opts = reframe.ReframeOptions(zoom=settings['zoom'], smooth_time=settings['smooth_time'], dead_zone=settings['dead_zone'], active_speaker=settings['active_speaker'])
    upscale = shorts.estimate_upscale(video['width'], video['height'], opts, settings['resolution'])
    wants_stack = settings['layout'] == 'stack' or (settings['layout'] == 'auto' and settings.get('stack_two_speakers') and settings.get('active_speaker'))
    layout, reason = shorts.choose_layout(settings['layout'], upscale, False, False) if not wants_stack else (None, None)
    info = {'upscale_factor':round(upscale, 3)}
    if layout == 'fit_blur':
        return [{'t':0,'x':.5,'y':.5}], 'center', dict(info, layout_used=layout), []
    detector = reframe.FaceDetector(media.storage_root()/'models', allow_download=os.environ.get('CLIP_NO_MODEL_DOWNLOAD') != '1')
    transcript = analysis.get_transcript(video['id']) if settings.get('active_speaker') else None
    words = [w for segment in (transcript or {}).get('segments', []) for w in segment.get('words', [])]
    speech = [(a,b) for a,b in reframe.speech_intervals(words) if b > start and a < end]
    samples,(w,h,fps) = reframe.analyze_subject(path,start,end,opts,detector,check,speech=speech)
    if not samples: raise ValueError('Could not read frames for subject tracking.')
    if not any(f[5] is not None for sample in samples for f in sample.faces): reframe.assign_tracks(samples,w)
    tracks = reframe.prominent_tracks(samples,end-start)
    if wants_stack:
        layout, reason = shorts.choose_layout(settings['layout'],upscale,settings.get('stack_two_speakers',False) and bool(speech),len(tracks)==2)
    info.update({'layout_used':layout,'detector':detector.kind,'tracks':len({f[5] for s in samples for f in s.faces if f[5] is not None}), 'speaker_status':'heuristic' if speech and opts.active_speaker else 'no_transcript' if opts.active_speaker else 'disabled'})
    if reason: info['layout_fallback_reason'] = reason
    progress(1)
    source = 'face' if any(s.faces for s in samples) else ('motion' if any(s.source == 'motion' for s in samples) else 'center')
    status = detector.kind + '_' + source + '_framing'
    if layout == 'fit_blur': return [{'t':0,'x':.5,'y':.5}],status,info,[]
    if layout == 'stack':
        panels = reframe.panel_paths(samples,tracks,w,h,fps,opts,int(round((end-start)*fps)))
        return [{'t':0,'x':.5,'y':.5}],status,info,panels
    cw,ch,camera = reframe.build_camera_path(samples,w,h,fps,opts,int(round((end-start)*fps)))
    if not camera: raise ValueError('Could not construct the camera path.')
    stride=max(1,(len(camera)+89)//90)
    indices=set(range(0,len(camera),stride))|{len(camera)-1}
    for sample in samples:
        if sample.cut:
            idx=min(len(camera)-1,max(0,round((sample.t-start)*fps))); indices.update((idx,max(0,idx-1)))
    points=[{'t':round(camera[i][0]-start,3),'x':round((camera[i][1]+cw/2)/w,5),'y':round((camera[i][2]+ch/2)/h,5)} for i in sorted(indices)]
    return points,status,info,[]


def full_frame_foreground(video, settings, width, height, bias, letterbox=False):
    """Shared fit-to-canvas sizing and scaling for full-frame layouts."""
    factor = min(width / video['width'], height / video['height'])
    if letterbox:
        rect = reframe.letterbox_rect(video['width'], video['height'], width, height, bias)
    else:
        fw=max(2, int(video['width']*factor)//2*2); fh=max(2, int(video['height']*factor)//2*2)
        rect={'w':fw,'h':fh,'x':(width-fw)//2,'y':max(0,min(height-fh,round((height-fh)/2+height*bias)))}
    flag = 'lanczos' if factor > 1 else 'area'
    amount = settings.get('sharpen', .35) if factor > 1.15 else 0
    sharp = f',unsharp=5:5:{amount}:5:5:0' if amount else ''
    return rect, f"scale={rect['w']}:{rect['h']}:flags={flag}{sharp}"


def filter_graph(video, settings, points, width, height, panels=None):
    layout = settings['layout']
    if layout in ('smart', 'center', 'follow'):
        cw,ch,pos = crop_with_offsets(points, video['width'], video['height'], settings)
        x = framing.expression(pos, 'x'); y = framing.expression(pos, 'y')
        sharp = settings.get('sharpen', .35) if layout == 'follow' and height/ch > 1.15 else 0
        sharpen = f',unsharp=5:5:{sharp}:5:5:0' if sharp else ''
        scale_flag = 'lanczos' if height/ch > 1 else 'area'
        return f"[0:v]setpts=PTS-STARTPTS,crop={cw}:{ch}:'{x}':'{y}',scale={width}:{height}:flags={scale_flag}{sharpen},setsar=1[v]"
    if layout == 'letterbox':
        # The existing FFmpeg composition path, without the blur branch: pad
        # makes a solid canvas and blits the scaled source at a fixed rectangle.
        bg=settings.get('letterbox_bg','#000000')
        if not isinstance(bg,str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}',bg): raise ValueError('Letterbox background must be a #RRGGBB color.')
        rect, foreground = full_frame_foreground(video,settings,width,height,settings.get('letterbox_vertical_bias',.02),letterbox=True)
        edge=''
        if settings.get('letterbox_edge'):
            edge=f',drawbox=x=0:y=0:w=iw:h=1:color=0x{bg[1:]}@0.5:t=fill,drawbox=x=0:y=ih-1:w=iw:h=1:color=0x{bg[1:]}@0.5:t=fill'
        return f"[0:v]setpts=PTS-STARTPTS,{foreground}{edge},pad={width}:{height}:{rect['x']}:{rect['y']}:color=0x{bg[1:]},setsar=1[v]"
    if layout == 'fit_blur':
        # Approximate the cover at 1/8 canvas resolution, then blur and darken.
        bw,bh=max(2,width//16*2),max(2,height//16*2)
        bias=settings.get('fit_vertical_bias',-.05)
        rect, foreground = full_frame_foreground(video,settings,width,height,bias)
        fw,fh,x,y=rect['w'],rect['h'],rect['x'],rect['y']
        shadow=max(2,round(width*.008))
        return (f"[0:v]setpts=PTS-STARTPTS,split=2[bg][fg];"
                f"[bg]scale={bw}:{bh}:force_original_aspect_ratio=increase:flags=area,crop={bw}:{bh},gblur=sigma=5,scale={width}:{height}:flags=bilinear,colorchannelmixer=rr=.65:gg=.65:bb=.65,drawbox=x={x+shadow}:y={y+shadow}:w={fw}:h={fh}:color=black@0.5:t=fill[back];"
                f"[fg]{foreground}[front];"
                f"[back][front]overlay={x}:{y},setsar=1[v]")
    if layout == 'stack':
        if not panels or len(panels)!=2: raise ValueError('Stack layout requires two valid face tracks.')
        chains=[]
        for i,panel in enumerate(panels):
            shifted=[{'t':p['t'],'x':framing.clamp(p['x']+settings.get('x_offset',0)*panel['crop_w'],0,video['width']-panel['crop_w']),'y':framing.clamp(p['y']+settings.get('y_offset',0)*panel['crop_h'],0,video['height']-panel['crop_h'])} for p in panel['points']]
            x=framing.expression(shifted,'x'); y=framing.expression(shifted,'y')
            sharp=settings.get('sharpen',.35) if (height/2)/panel['crop_h']>1.15 else 0
            sharpen=f',unsharp=5:5:{sharp}:5:5:0' if sharp else ''
            scale_flag='lanczos' if height/2/panel['crop_h']>1 else 'area'
            chains.append(f"[s{i}]crop={panel['crop_w']}:{panel['crop_h']}:'{x}':'{y}',scale={width}:{height//2}:flags={scale_flag}{sharpen},setsar=1[p{i}]")
        return '[0:v]setpts=PTS-STARTPTS,split=2[s0][s1];'+';'.join(chains)+';[p0][p1]vstack=inputs=2[v]'
    if layout == 'blur':
        return f"[0:v]setpts=PTS-STARTPTS,split=2[bg][fg];[bg]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},boxblur=20:2[back];[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[front];[back][front]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
    top = int(height * settings['split_ratio']) // 2 * 2; bottom = height - top
    return f"[0:v]setpts=PTS-STARTPTS,split=2[main][secondary];[main]scale={width}:{top}:force_original_aspect_ratio=decrease,pad={width}:{top}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[a];[secondary]scale={width}:{bottom}:force_original_aspect_ratio=increase,crop={width}:{bottom},setsar=1[b];[a][b]vstack=inputs=2[v]"


def preview_path(clip, settings):
    video,path=source_video(clip['video_id']); details=media.probe_video(path)
    start,end=apply_range(clip['start'],clip['end'],details['duration'],settings,settings.get('pad',0))
    if end-start>shorts.MAX_DURATION: raise ValueError('Preview is limited to 180 seconds.')
    transcript=analysis.get_transcript(video['id']) if settings.get('active_speaker') else None
    identity=[clip['id'],start,end,path.stat().st_mtime_ns,path.stat().st_size,settings,transcript]
    key=hashlib.sha256(json.dumps(identity,sort_keys=True,default=str).encode()).hexdigest()
    with _preview_lock:
        if key in _preview_paths:
            _preview_paths.move_to_end(key)
            return _preview_paths[key]
    panels=[]; layout=settings['layout']
    if layout in ('auto','follow','fit_blur','stack'):
        points,status,info,panels=shorts_track(details | {'id':video['id']},path,start,end,settings,lambda:None,lambda p:None)
        layout=info['layout_used']
    elif layout=='smart':
        points,status=track(video,path,start,end,settings,lambda:None,lambda p:None)
    else: points=[{'t':0,'x':.5,'y':.5}]; status='center'
    w,h=details['width'],details['height']
    if layout in ('fit_blur','blur','letterbox'):
        cw,ch=w,h; pos=[{'t':0,'x':0,'y':0}]
    elif layout=='stack' and panels:
        panel=panels[0]; cw,ch=panel['crop_w'],panel['crop_h']
        pos=[{'t':p['t'],'x':framing.clamp(p['x']+settings.get('x_offset',0)*cw,0,w-cw),'y':framing.clamp(p['y']+settings.get('y_offset',0)*ch,0,h-ch)} for p in panel['points']]
    else: cw,ch,pos=crop_with_offsets(points,w,h,settings)
    times=([0] if layout=='letterbox' else np.arange(0,end-start,.1).tolist()+[end-start])
    xp=[p['t'] for p in pos]
    xs=np.interp(times,xp,[p['x'] for p in pos]); ys=np.interp(times,xp,[p['y'] for p in pos])
    result={'width':w,'height':h,'layout':layout,'source_duration':details['duration'],'start':start,'end':end,'path':[{'t':round(start+t,3),'x':round(float(x),2),'y':round(float(y),2),'w':cw,'h':ch} for t,x,y in zip(times,xs,ys)]}
    if layout == 'letterbox':
        canvas_h = shorts.RECOMMENDED_HEIGHT if settings.get('preset')=='shorts' else settings['resolution']
        canvas_w = canvas_h*9//16
        result.update({'canvas_width':canvas_w,'canvas_height':canvas_h,'video_rect':reframe.letterbox_rect(w,h,canvas_w,canvas_h,settings.get('letterbox_vertical_bias',.02)),'letterbox_bg':settings.get('letterbox_bg','#000000')})
    with _preview_lock:
        _preview_paths[key]=result
        while len(_preview_paths)>32: _preview_paths.popitem(last=False)
    return result


def preview(clip, settings):
    video, path = source_video(clip['video_id'])
    details = media.probe_video(path)
    preview_settings = dict(settings)
    panels = []
    if settings['layout'] in ('auto','follow','fit_blur','stack'):
        points, status, info, panels = shorts_track(details | {'id': video['id']}, path, clip['start'], clip['end'], settings, lambda: None, lambda p: None)
        preview_settings['layout'] = info['layout_used']
    elif settings['layout'] == 'smart' and settings['track_faces']:
        points, status = framing.detect_frames(path, clip['start'], .1, 1.0)
    else: points, status = [{'t':0,'x':.5,'y':.5}], 'center'
    graph = filter_graph(details, preview_settings, points, 270, 480, panels)
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


def measure_loudness(path, start, duration, rid, target, intervals=None):
    """Measure without exposing FFmpeg diagnostics or paths; honor cancellation."""
    args=[os.environ.get('CLIP_FFMPEG','ffmpeg'),'-hide_banner','-nostdin','-ss',str(start),'-protocol_whitelist','file,pipe','-i',str(path)]
    loudnorm=f'loudnorm=I={target}:TP={shorts.TARGET_TRUE_PEAK}:LRA=11:print_format=json'
    if intervals:
        n=len(intervals); graph=['[0:a]asplit='+str(n)+''.join(f'[s{i}]' for i in range(n))]
        for i,(a,b) in enumerate(intervals):
            length=b-a
            fade_in=',afade=t=in:st=0:d=0.03' if i else ''
            fade_out=f',afade=t=out:st={max(0,length-.03):.6f}:d=0.03' if i<n-1 else ''
            graph.append(f'[s{i}]atrim=start={max(0,a-start):.6f}:end={max(0,b-start):.6f},asetpts=PTS-STARTPTS{fade_in}{fade_out}[sout{i}]')
        graph.append(''.join(f'[sout{i}]' for i in range(n))+f'concat=n={n}:v=0:a=1[join]')
        graph.append('[join]'+loudnorm+'[measure]')
        args+=['-filter_complex',';'.join(graph),'-map','[measure]']
    else: args+=['-t',str(duration),'-vn','-af',loudnorm]
    args+=['-f','null','-']
    try: proc=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    except OSError: return None
    try:
        deadline=time.monotonic()+max(60,min(600,duration*4))
        while True:
            check_cancel(rid)
            try:
                _,stderr=proc.communicate(timeout=.2); break
            except subprocess.TimeoutExpired:
                if time.monotonic()>deadline: return None
        if proc.returncode: return None
        matches=re.findall(r'\{[^{}]*\}',stderr)
        if not matches: return None
        values=json.loads(matches[-1])
        for key in ('input_i','input_tp','input_lra','input_thresh','target_offset'):
            number=float(values[key])
            if not math.isfinite(number): return None
            values[key]=number
        return values
    except (ValueError,KeyError,TypeError): return None
    finally:
        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()


def loudnorm_filter(measured, target):
    return (f'loudnorm=I={target}:TP={shorts.TARGET_TRUE_PEAK}:LRA=11:'
            f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
            f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
            f"offset={measured['target_offset']}:linear=true:print_format=summary")


def cut_filter(intervals, clip_start, has_audio, audio_filter=None):
    """Trim a finished full-range camera path and the source audio on one timeline."""
    n=len(intervals); pieces=[]
    pieces.append('[base]split='+str(n)+''.join(f'[vsrc{i}]' for i in range(n)))
    if has_audio: pieces.append('[0:a]asplit='+str(n)+''.join(f'[asrc{i}]' for i in range(n)))
    for i,(a,b) in enumerate(intervals):
        start=max(0,a-clip_start); end=max(start,b-clip_start); length=end-start
        pieces.append(f'[vsrc{i}]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[vt{i}]')
        if has_audio:
            fade_in=',afade=t=in:st=0:d=0.03' if i else ''
            fade_out=f',afade=t=out:st={max(0,length-.03):.6f}:d=0.03' if i<n-1 else ''
            pieces.append(f'[asrc{i}]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS{fade_in}{fade_out}[at{i}]')
    pieces.append(''.join(f'[vt{i}]' for i in range(n))+f'concat=n={n}:v=1:a=0[v]')
    if has_audio:
        pieces.append(''.join(f'[at{i}]' for i in range(n))+f'concat=n={n}:v=0:a=1[acut]')
        audio_chain=(audio_filter+',' if audio_filter else '')+'aformat=channel_layouts=stereo,aresample=48000,asetpts=N/SR/TB'
        pieces.append('[acut]'+audio_chain+'[a]')
    return ';'.join(pieces)


def run_job(job):
    rid = job['id']; temp = media.storage_path('temp', job['project_id'], 'render-' + rid)
    target = media.storage_path('rendered', job['project_id'], rid + '.mp4')
    thumb = media.storage_path('rendered', job['project_id'], rid + '.jpg')
    sidecar_text = target.with_suffix('.txt'); sidecar_json = target.with_suffix('.metadata.json')
    try:
        check_cancel(rid); video, path = source_video(job['video_id']); settings = json.loads(job['settings_json'])
        details = media.probe_video(path)
        clip_start_original=settings.get('_source_clip_start',job['start']); clip_end_original=settings.get('_source_clip_end',job['end'])
        temp.mkdir(parents=True, exist_ok=True); target.parent.mkdir(parents=True, exist_ok=True)
        update(rid, 'Detecting faces', 0)
        panels = []
        if settings['layout'] in ('auto','follow','fit_blur','stack'):
            points, status, info, panels = shorts_track(details | {'id':video['id']}, path, job['start'], job['end'], settings, lambda: check_cancel(rid), lambda p: update(rid, 'Detecting faces', p * 20))
            render_settings = dict(settings, layout=info['layout_used'])
        elif settings['layout']=='letterbox':
            points, status, info = [{'t':0,'x':.5,'y':.5}], 'center', {}
            render_settings = settings
        else:
            points, status, info = track(video, path, job['start'], job['end'], settings, lambda: check_cancel(rid), lambda p: update(rid, 'Detecting faces', p * 20), with_info=True)
            render_settings = settings
        if settings['layout']=='letterbox':
            rect=reframe.letterbox_rect(details['width'],details['height'],job['width'],job['height'],settings.get('letterbox_vertical_bias',.02))
            info.update({'layout_used':'letterbox','video_rect':rect,'letterbox_bg':settings.get('letterbox_bg','#000000')})
            if rect['w']==job['width'] and rect['h']==job['height']: info['letterbox_note']='source already fills the frame'
        info.update({'start_offset':round(job['start']-clip_start_original,3),'end_offset':round(job['end']-clip_end_original,3),'x_offset':settings.get('x_offset',0),'y_offset':settings.get('y_offset',0)})
        check_cancel(rid); update(rid, 'Rendering video', 20)
        output = temp / 'output.mp4'; graph = filter_graph(details, render_settings, points, job['width'], job['height'], panels)
        preset = settings.get('preset', 'legacy')
        fps = shorts.output_fps(details['frame_rate']) if preset == 'shorts' else None
        codec = shorts.video_encoder_args() if preset == 'shorts' else encoder_args(settings['quality'])
        transcript = analysis.get_transcript(job['video_id']) or {}
        words = [w for segment in transcript.get('segments', []) for w in segment.get('words', [])]
        snapped_end = tighten.snap_ending(words, job['end']) if preset == 'shorts' and words else job['end']
        if snapped_end <= job['start']: snapped_end = job['end']
        snapped = snapped_end < job['end'] - 1e-6
        intervals = [(job['start'],snapped_end)]
        if preset == 'shorts' and settings.get('tighten', True) and words:
            intervals = tighten.plan_cuts(words,job['start'],snapped_end,trim_leading_fillers=settings.get('trim_fillers',True),max_pause=settings.get('max_pause',.6))
        duration=sum(b-a for a,b in intervals)
        # Save the exact edited word timeline with the render, so later transcript
        # edits or reanalysis cannot silently desynchronize its downloads.
        snapshot_words = [{k:w[k] for k in ('start','end','word')} for w in tighten.remap_words(words,intervals)]
        cut = len(intervals)>1 or abs(intervals[0][0]-job['start'])>1e-6 or abs(intervals[-1][1]-job['end'])>1e-6
        if preset == 'shorts': info.update({'duration':round(duration,3), 'removed_seconds':round(job['end']-job['start']-duration,3)})
        measured = None
        if preset == 'shorts':
            info.update({'preset':'shorts','fps':fps,'loudness':{'before':None,'after':None}})
            if video['has_audio']:
                update(rid, 'Measuring audio loudness', 18)
                measured = measure_loudness(path, job['start'], job['end']-job['start'], rid, settings.get('loudness_lufs', shorts.TARGET_LUFS), intervals if cut else None)
                if measured: info['loudness']['before']=measured['input_i']
                else: info.setdefault('warnings',[]).append('Loudness measurement failed; audio was not normalized.')
        loop = settings.get('loop_polish',False)
        fade_in = .1 if loop else 0
        fade_out = max(.15 if snapped else 0, .1 if loop else 0)
        audio_filters = []
        if measured: audio_filters.append(loudnorm_filter(measured, settings.get('loudness_lufs', shorts.TARGET_LUFS)))
        elif preset != 'shorts' and settings.get('normalize_audio') and video['has_audio']: audio_filters.append('loudnorm=I=-16:TP=-1.5:LRA=11')
        if fade_in: audio_filters.append(f'afade=t=in:st=0:d={min(fade_in,duration):.3f}')
        if fade_out: audio_filters.append(f'afade=t=out:st={max(0,duration-fade_out):.3f}:d={min(fade_out,duration):.3f}')
        if snapped: info['snapped_end']=round(snapped_end,3)
        if cut:
            audio_filter = ','.join(audio_filters) or None
            graph = graph.removesuffix('[v]') + '[base];' + cut_filter(intervals, job['start'], bool(video['has_audio']), audio_filter)
        if loop:
            before_video, after_video = graph.rsplit('[v]',1)
            graph = before_video + '[loopbase]' + after_video + f';[loopbase]fade=t=in:st=0:d=0.1,fade=t=out:st={max(0,duration-.1):.3f}:d=0.1[v]'
        if settings.get('captions'):
            caption_words = tighten.remap_words(words,intervals) if cut else words
            caption_start, caption_end = (0, duration) if cut else (job['start'],job['end'])
            requested=settings.get('caption_position') or ('on_video' if info.get('layout_used')=='letterbox' else 'safe_zone')
            caption_layout=None
            if requested=='on_video':
                rect=info.get('video_rect',{'x':0,'y':0,'w':job['width'],'h':job['height']})
                caption_layout=captions.on_video_layout(job['width'],job['height'],rect,settings.get('caption_style','highlight'))
            info['caption_position_used']=caption_layout['used'] if caption_layout else 'safe_zone'
            content = captions.build_ass(caption_words, caption_start, caption_end, job['width'], job['height'], settings.get('caption_style', 'highlight'), settings.get('caption_uppercase', False), settings.get('caption_safe_zone', True) or requested=='on_video', caption_layout)
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
            before_video, after_video = graph.rsplit('[v]',1)
            graph = before_video + '[captionbase]' + after_video + ';[captionbase]ass=filename=captions.ass' + font_option + '[v]'
        graph_path = temp / 'filter.ffscript'
        graph_path.write_text(graph, encoding='utf-8')
        out_args = shorts.output_args(fps) if preset == 'shorts' else ['-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart']
        args = [os.environ.get('CLIP_FFMPEG','ffmpeg'),'-hide_banner','-loglevel','error','-nostdin','-y','-ss',str(job['start']),'-protocol_whitelist','file,pipe','-i',str(path),'-t',str(job['end']-job['start']),'-filter_complex_script',str(graph_path),'-map','[v]','-map',('[a]' if cut and video['has_audio'] else '0:a:0?'),*codec,*out_args,'-progress','pipe:1','-nostats',str(output)]
        if audio_filters and not cut and video['has_audio']:
            args[-1:-1] = ['-af', ','.join(audio_filters)]
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
                or not probe['duration'] or abs(probe['duration'] - duration) > max(.5, duration * .02)):
            raise ValueError('Rendered output failed validation (dimensions, codecs or duration).')
        if cut and video['has_audio']:
            vd,ad=probe.get('video_duration'),probe.get('audio_duration')
            if vd is None or ad is None or abs(vd-ad)>.08:
                raise ValueError('Edited output failed audio/video synchronization validation.')
        if preset == 'shorts' and measured:
            after=measure_loudness(output, 0, duration, rid, settings.get('loudness_lufs', shorts.TARGET_LUFS))
            if after:
                info['loudness']['after']=after['input_i']
                if abs(after['input_i'] - settings.get('loudness_lufs', shorts.TARGET_LUFS)) > 1.5:
                    info.setdefault('warnings',[]).append('Output loudness differs from the target; review the audio.')
            else: info.setdefault('warnings',[]).append('Could not measure output loudness.')
        temporary_thumb = temp / 'thumbnail.jpg'
        has_thumb = media.make_thumbnail(output, temporary_thumb)
        check_cancel(rid)
        with database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT cancel_requested FROM renders WHERE id=?', (rid,)).fetchone()
            if not current or current['cancel_requested'] or _stop.is_set(): raise Cancelled()
            meta=db.execute('SELECT titles_json,description,hashtags_json,provider,created_at FROM clip_metadata WHERE clip_id=?',(job['clip_id'],)).fetchone()
            if meta:
                payload={'titles':json.loads(meta['titles_json']), 'description':meta['description'], 'hashtags':json.loads(meta['hashtags_json']), 'provider':meta['provider'], 'created_at':meta['created_at']}
                (temp/'metadata.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                (temp/'metadata.txt').write_text('\n'.join(payload['titles'])+'\n\n'+payload['description']+'\n\n'+' '.join(payload['hashtags'])+'\n',encoding='utf-8')
            output.replace(target)
            if has_thumb: temporary_thumb.replace(thumb)
            if meta:
                (temp/'metadata.txt').replace(sidecar_text)
                (temp/'metadata.json').replace(sidecar_json)
            db.execute("UPDATE renders SET status='completed',stage='Render completed',progress=100,path=?,thumbnail_path=?,tracking_status=?,info_json=?,caption_words_json=?,completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (str(target), str(thumb) if has_thumb else None, status, json.dumps(info), json.dumps(snapshot_words), rid))
    except Cancelled:
        target.unlink(missing_ok=True); thumb.unlink(missing_ok=True); sidecar_text.unlink(missing_ok=True); sidecar_json.unlink(missing_ok=True)
        with database.connect() as db: db.execute("UPDATE renders SET status='cancelled',stage='Cancelled',error='Render cancelled or interrupted.',completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (rid,))
    except Exception as exc:
        target.unlink(missing_ok=True); thumb.unlink(missing_ok=True); sidecar_text.unlink(missing_ok=True); sidecar_json.unlink(missing_ok=True)
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
        media.storage_path('rendered',row['project_id'],row['id']+'.txt').unlink(missing_ok=True)
        media.storage_path('rendered',row['project_id'],row['id']+'.metadata.json').unlink(missing_ok=True)
    if os.environ.get('CLIP_DISABLE_WORKER') != '1':
        _worker = threading.Thread(target=loop, name='render-worker', daemon=True); _worker.start()


def stop_worker():
    _stop.set()
    if _worker: _worker.join(timeout=5)
