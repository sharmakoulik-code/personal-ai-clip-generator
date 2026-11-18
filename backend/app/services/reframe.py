"""Local subject-aware camera paths; no speech inference is made here."""
import hashlib
import math
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

YUNET_URL = 'https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx'
YUNET_SHA256 = '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4'


@dataclass
class ReframeOptions:
    aspect: float = 9 / 16
    layout: str = 'follow'
    letterbox_bg: str = '#000000'
    letterbox_vertical_bias: float = .02
    letterbox_edge: bool = False
    fit_vertical_bias: float = -.05
    sharpen: float = .35
    zoom: float = 1.0
    sample_fps: float = 6.0
    dead_zone: float = .12
    smooth_time: float = .55
    max_speed: float = .45
    headroom: float = .38
    cut_threshold: float = .55
    lost_hold: float = 1.2
    min_face: float = .04
    active_speaker: bool = True
    switch_ratio: float = 1.4
    min_dwell: float = 1.5
    group_fit: float = .85


@dataclass
class Sample:
    t: float
    cx: float | None = None
    cy: float | None = None
    size: float = 0.0
    cut: bool = False
    source: str = 'none'
    faces: list = field(default_factory=list)
    speaker_id: int | None = None


def letterbox_rect(src_w, src_h, out_w=1080, out_h=1920, bias=.02):
    """Fit the entire image with even dimensions and chroma-aligned placement."""
    if any(not isinstance(n,(int,float)) or not math.isfinite(n) or n<=0 for n in (src_w,src_h,out_w,out_h)) or not math.isfinite(bias) or not -.15<=bias<=.15:
        raise ValueError('Invalid letterbox dimensions or vertical bias.')
    if out_w%2 or out_h%2: raise ValueError('Letterbox canvas dimensions must be even.')
    scale=min(out_w/src_w,out_h/src_h)
    fw=min(out_w,max(2,round(src_w*scale/2)*2)); fh=min(out_h,max(2,round(src_h*scale/2)*2))
    x=max(0,min(out_w-fw,round((out_w-fw)/4)*2))
    y=max(0,min(out_h-fh,round(((out_h-fh)/2+bias*out_h)/2)*2))
    return {'x':int(x),'y':int(y),'w':int(fw),'h':int(fh)}


def _yunet_path(models_dir: Path, allow_download: bool) -> Path | None:
    path = models_dir / 'face_detection_yunet.onnx'
    def valid(p):
        return p.is_file() and p.stat().st_size <= 5_000_000 and hashlib.sha256(p.read_bytes()).hexdigest() == YUNET_SHA256
    if valid(path): return path
    if not allow_download: return None
    tmp = path.with_suffix('.part')
    try:
        models_dir.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(YUNET_URL, timeout=30) as response, tmp.open('wb') as out:
            data = response.read(5_000_001)
            if len(data) > 5_000_000: return None
            out.write(data)
        if not valid(tmp): return None
        tmp.replace(path)
        return path
    except (OSError, ValueError):
        return None
    finally:
        tmp.unlink(missing_ok=True)


class FaceDetector:
    """YuNet when available; explicitly report Haar fallback."""
    def __init__(self, models_dir=None, allow_download=True):
        self.kind = 'haar'; self._yunet = None; self._size = None
        self._clahe = cv2.createCLAHE(2.0, (8, 8))
        path = _yunet_path(Path(models_dir), allow_download) if models_dir else None
        if path and hasattr(cv2, 'FaceDetectorYN'):
            try:
                self._yunet = cv2.FaceDetectorYN.create(str(path), '', (320, 320), .6, .3, 20)
                self.kind = 'yunet'
            except cv2.error: pass
        self._haar = None
        if self._yunet is None:
            self._haar = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / 'haarcascade_frontalface_default.xml'))
            if self._haar.empty(): raise ValueError('Face detector data is missing from OpenCV.')

    def detect(self, frame):
        h, w = frame.shape[:2]
        scale = min(1., 640 / max(w, h))
        small = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1 else frame
        sh, sw = small.shape[:2]
        if self._yunet is not None:
            if self._size != (sw, sh): self._yunet.setInputSize((sw, sh)); self._size = (sw, sh)
            _, faces = self._yunet.detect(small)
            boxes = [(f[0], f[1], f[2], f[3], float(f[14])) for f in (faces if faces is not None else [])]
        else:
            gray = self._clahe.apply(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
            boxes = [(x,y,fw,fh,.7) for x,y,fw,fh in self._haar.detectMultiScale(gray, 1.1, 5, minSize=(max(24, sw // 25),)*2)]
        return [(x/scale,y/scale,w/scale,h/scale,c) for x,y,w,h,c in boxes]


def _hist(frame):
    hsv = cv2.cvtColor(cv2.resize(frame, (96, 54)), cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0,1], None, [16,8], [0,180,0,256])
    return cv2.normalize(hist, hist).flatten()


def motion_centroid(prev_gray, gray):
    if prev_gray is None: return None
    diff = cv2.GaussianBlur(cv2.absdiff(prev_gray, gray), (9,9), 0)
    mask = diff > 18
    if mask.mean() < .002 or mask.mean() > .6: return None
    ys, xs = np.nonzero(mask)
    return float(np.median(xs)), float(np.median(ys))


def pick_subject(faces, prev, width):
    if not faces: return None
    def score(f):
        x,y,w,h,c = f; size = w*h*c
        if prev is not None:
            dist = math.hypot(x+w/2-prev[0], y+h/2-prev[1])/width
            size *= 1 + max(0.,1-dist*4)*2.5
        return size
    return max(faces, key=score)


def analyze_subject(path, start, end, opts, detector, check_cancel=lambda: None, speech=None):
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened(): raise ValueError('Could not open the video for subject tracking.')
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.
    width,height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if not math.isfinite(fps) or fps <= 0 or width <= 0 or height <= 0:
        cap.release(); raise ValueError('Invalid source dimensions or frame rate.')
    step = max(1, round(fps/opts.sample_fps)); cap.set(cv2.CAP_PROP_POS_MSEC,start*1000)
    samples=[]; prev_hist=prev_gray=prev_pos=prev_frame_gray=None; index=0; next_sample=0
    try:
        while start+index/fps < end:
            if index % 15 == 0: check_cancel()
            if not cap.grab(): break
            if index >= next_sample:
                ok,frame=cap.retrieve()
                if not ok: break
                hist=_hist(frame)
                cut=prev_hist is not None and cv2.compareHist(prev_hist,hist,cv2.HISTCMP_BHATTACHARYYA)>opts.cut_threshold
                if cut: prev_pos=prev_gray=prev_frame_gray=None
                prev_hist=hist
                faces=[f for f in detector.detect(frame) if f[2] >= opts.min_face*width]
                gray=cv2.cvtColor(cv2.resize(frame,(160,max(1,int(160*height/width)))),cv2.COLOR_BGR2GRAY)
                full_gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
                face_data=[]
                for x,y,fw,fh,confidence in faces:
                    left=max(0,int(x+fw*.2)); right=min(width,int(x+fw*.8))
                    top=max(0,int(y+fh*.6)); bottom=min(height,int(y+fh))
                    activity=0.
                    if prev_frame_gray is not None and right>left and bottom>top:
                        now=cv2.resize(full_gray[top:bottom,left:right],(32,16)).astype(np.float32)
                        before=cv2.resize(prev_frame_gray[top:bottom,left:right],(32,16)).astype(np.float32)
                        activity=float(np.mean(np.abs(now-before))/max(10.,float(np.mean(now))))
                    face_data.append((float(x),float(y),float(fw),float(fh),float(confidence),None,activity))
                prev_frame_gray=full_gray
                # Higher sampling when several faces are visible and speech is available.
                step = max(1, math.floor(fps/10)) if speech and opts.active_speaker and len(faces)>=2 else max(1,round(fps/opts.sample_fps))
                next_sample=index+step
                chosen=pick_subject(faces,prev_pos,width)
                if chosen:
                    x,y,w,h,_=chosen; s=Sample(start+index/fps,x+w/2,y+h/2,w,cut,'face'); prev_pos=(s.cx,s.cy)
                else:
                    m=motion_centroid(prev_gray,gray)
                    s=Sample(start+index/fps,m[0]*width/gray.shape[1],m[1]*height/gray.shape[0],0.,cut,'motion') if m else Sample(start+index/fps,cut=cut)
                s.faces=face_data
                prev_gray=gray; samples.append(s)
            index+=1
    finally: cap.release()
    if speech and opts.active_speaker:
        assign_tracks(samples,width)
        choose_speaker(samples,speech,opts,min(width,height*opts.aspect/opts.zoom))
    return samples,(width,height,fps)


def speech_intervals(words, merge_gap=.35):
    intervals=[]
    for word in sorted(words, key=lambda w: w.get('start', 0)):
        try: start,end=float(word['start']),float(word['end'])
        except (KeyError, TypeError, ValueError): continue
        if not math.isfinite(start) or not math.isfinite(end) or end <= start: continue
        if intervals and start <= intervals[-1][1] + merge_gap:
            intervals[-1] = (intervals[-1][0],max(end,intervals[-1][1]))
        else: intervals.append((start,end))
    return intervals


def assign_tracks(samples, width):
    """Greedy one-to-one face association, expiring after one second."""
    tracks={}; next_id=0
    for sample in samples:
        if sample.cut: tracks.clear()
        tracks={tid: state for tid,state in tracks.items() if sample.t-state[2] <= 1.0}
        edges=[]
        for i,face in enumerate(sample.faces):
            x,y,w,h,_,_,_=face; center=(x+w/2,y+h/2)
            for tid,(old,oldsize,_) in tracks.items():
                ratio=w/oldsize if oldsize else 0
                dist=math.hypot(center[0]-old[0],center[1]-old[1])
                if dist < .15*width and .6 <= ratio <= 1.6: edges.append((dist,i,tid))
        assigned={}; used=set()
        for _,i,tid in sorted(edges):
            if i not in assigned and tid not in used: assigned[i]=tid; used.add(tid)
        updated=[]
        for i,face in enumerate(sample.faces):
            x,y,w,h,c,_,activity=face
            tid=assigned.get(i)
            if tid is None: tid=next_id; next_id+=1
            tracks[tid]=((x+w/2,y+h/2),w,sample.t)
            updated.append((x,y,w,h,c,tid,activity))
        sample.faces=updated
    return next_id


def choose_speaker(samples, speech, opts, crop_width):
    """Use mouth motion only during speech; grouping and switching are heuristic."""
    scores={}; current=None; last_switch=-float('inf'); challenger=None; challenge_since=None; prev_t=None; switches=0
    for sample in samples:
        if sample.cut:
            scores={}; current=challenger=None; challenge_since=None; prev_t=None; last_switch=sample.t
        faces=sample.faces
        if len(faces) <= 1:
            # Preserve the visual subject path on single-face and no-face frames.
            if len(faces)==1: current=faces[0][5]
            prev_t=sample.t
            continue
        dt = max(.001,sample.t-prev_t) if prev_t is not None else .1
        prev_t=sample.t
        speaking=any(a <= sample.t <= b for a,b in speech)
        if speaking:
            alpha=1-math.exp(-dt/1.5)
            for face in faces:
                tid=face[5]; activity=max(0.,face[6]); scores[tid]=scores.get(tid,0.)*(1-alpha)+activity*alpha
        by_id={f[5]:f for f in faces}
        if current not in by_id:
            current=max(faces,key=lambda f:(scores.get(f[5],0),f[2]*f[3]))[5]
            last_switch=sample.t; challenger=None; challenge_since=None
        best=max(faces,key=lambda f:scores.get(f[5],0))[5]
        if speaking and best != current and scores.get(best,0) > max(.0001, scores.get(current,0)*opts.switch_ratio):
            if challenger != best: challenger=best; challenge_since=sample.t
            if sample.t-challenge_since>=.7 and sample.t-last_switch>=opts.min_dwell:
                current=best; last_switch=sample.t; switches+=1; challenger=None; challenge_since=None
        else: challenger=None; challenge_since=None
        sample.speaker_id=current
        face=by_id[current]; other=min((f for f in faces if f[5]!=current), key=lambda f:abs((f[0]+f[2]/2)-(face[0]+face[2]/2)), default=None)
        if other and abs((other[0]+other[2]/2)-(face[0]+face[2]/2))<=opts.group_fit*crop_width:
            sample.cx=(face[0]+face[2]/2+other[0]+other[2]/2)/2
            sample.cy=(face[1]+face[3]/2+other[1]+other[3]/2)/2
            sample.source='group'
        else:
            sample.cx=face[0]+face[2]/2; sample.cy=face[1]+face[3]/2; sample.size=face[2]; sample.source='speaker_heuristic'
    return switches


def prominent_tracks(samples, duration, min_visibility=.6):
    """Rank distinct tracks by visible face-area-time and require persistence."""
    stats = {}
    if not samples or duration <= 0: return []
    for i, sample in enumerate(samples):
        dt = min(1., max(0., (samples[i+1].t-sample.t) if i+1 < len(samples) else duration/len(samples)))
        for x,y,w,h,conf,tid,activity in sample.faces:
            if tid is None: continue
            state = stats.setdefault(tid, {'visible': 0., 'area': 0., 'x': 0.})
            state['visible'] += dt; state['area'] += w*h*dt; state['x'] += (x+w/2)*dt
    ranked = sorted(stats.items(), key=lambda item: item[1]['area'], reverse=True)[:2]
    if len(ranked) < 2 or any(data['visible'] / duration < min_visibility for _,data in ranked): return []
    return sorted(ranked, key=lambda item: item[1]['x']/item[1]['visible'])


def panel_paths(samples, tracks, width, height, fps, opts, frame_count):
    from dataclasses import replace
    panel_opts=replace(opts, aspect=9/8)
    output=[]
    for tid,_ in tracks:
        selected=[]
        for sample in samples:
            face=next((f for f in sample.faces if f[5]==tid), None)
            if face:
                x,y,w,h,*_=face
                selected.append(Sample(sample.t,x+w/2,y+h/2,w,sample.cut,'face'))
            else: selected.append(Sample(sample.t,cut=sample.cut))
        cw,ch,path=build_camera_path(selected,width,height,fps,panel_opts,frame_count)
        if not path: raise ValueError('Could not construct a stacked face track.')
        stride=max(1,(len(path)+89)//90)
        indices=set(range(0,len(path),stride))|{len(path)-1}
        for sample in samples:
            if sample.cut:
                idx=min(len(path)-1,max(0,round((sample.t-samples[0].t)*fps)))
                indices.update((idx,max(0,idx-1)))
        output.append({'crop_w':int(cw)//2*2,'crop_h':int(ch)//2*2,
                       'points':[{'t':round(path[i][0]-samples[0].t,3), 'x':round(path[i][1],2), 'y':round(path[i][2],2)} for i in sorted(indices)]})
    return output


def _median(values,k=5):
    half=k//2
    return [float(np.median(values[max(0,i-half):i+half+1])) for i in range(len(values))]


def smooth_scene(times, targets, span, opts, fps):
    if not times: return []
    n=max(1,int(round((times[-1]-times[0])*fps))+1)
    target=np.interp([times[0]+i/fps for i in range(n)],times,targets)
    cam=float(target[0]); vel=0.; omega=2/max(.05,opts.smooth_time); limit=opts.max_speed*span; zone=opts.dead_zone*span/2; dt=1/fps; out=[]
    for goal in target:
        err=goal-cam; aim=cam if abs(err)<=zone else goal-math.copysign(zone*.5,err)
        accel=omega*omega*(aim-cam)-2*omega*vel
        vel=max(-limit,min(limit,vel+accel*dt)); cam+=vel*dt; out.append(cam)
    return out


def _fill_gaps(samples,opts,default_x,default_y):
    xs=[]; ys=[]; sizes=[]; last=None; scene_start=0
    for i,s in enumerate(samples):
        if i == 0 or s.cut:
            last=None; scene_start=i
        if s.cx is not None:
            last=(s.cx,s.cy,s.size,s.t); xs.append(s.cx); ys.append(s.cy); sizes.append(s.size)
        elif last and s.t-last[3]<=opts.lost_hold:
            xs.append(last[0]); ys.append(last[1]); sizes.append(last[2])
        elif i != scene_start:
            xs.append(xs[-1]); ys.append(ys[-1]); sizes.append(0.)
        else:
            # A cut with no visible subject must not jump to an earlier scene's subject.
            xs.append(default_x); ys.append(default_y); sizes.append(0.)
    return xs,ys,sizes


def build_camera_path(samples,src_w,src_h,fps,opts,n_frames=None):
    crop_w=min(src_w,src_h*opts.aspect)/opts.zoom; crop_h=crop_w/opts.aspect
    if not samples: return crop_w,crop_h,[]
    xs,ys,_=_fill_gaps(samples,opts,src_w/2,src_h/2)
    scenes=[]; current=[]
    for i,s in enumerate(samples):
        if s.cut and current: scenes.append(current); current=[]
        current.append(i)
    scenes.append(current); path=[]
    for k,idxs in enumerate(scenes):
        t=[samples[i].t for i in idxs]; x=_median([xs[i] for i in idxs]); y=_median([ys[i] for i in idxs])
        y_target=[yy-(opts.headroom-.5)*crop_h for yy in y]
        next_start=samples[scenes[k+1][0]].t if k+1<len(scenes) else None
        if next_start is not None: t.append(next_start); x.append(x[-1]); y_target.append(y_target[-1])
        elif len(t)==1: t.append(t[0]+1/fps); x.append(x[-1]); y_target.append(y_target[-1])
        cam_x=smooth_scene(t,x,crop_w,opts,fps)
        cam_y=smooth_scene(t,y_target,crop_h,opts,fps) if crop_h<src_h-1 else [src_h/2]*len(cam_x)
        count=len(cam_x)-(1 if next_start is not None else 0)
        for i,(cx,cy) in enumerate(zip(cam_x[:count],cam_y[:count])):
            x0=min(max(0.,cx-crop_w/2),src_w-crop_w); y0=min(max(0.,cy-crop_h/2),src_h-crop_h)
            path.append((t[0]+i/fps,x0,y0))
    if n_frames is not None and path:
        # Keep strictly increasing timestamps for later interpolation.
        path=path[:n_frames]
        while len(path)<n_frames: path.append((path[-1][0]+1/fps,path[-1][1],path[-1][2]))
    return crop_w,crop_h,path
