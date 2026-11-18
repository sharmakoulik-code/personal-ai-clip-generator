"""Fast, local and explicitly heuristic source checks for Shorts."""
import math
from fractions import Fraction

import cv2
import numpy as np

from . import media, reframe, shorts


def frame_rate(value):
    try:
        result = float(Fraction(str(value)))
        return result if math.isfinite(result) and 0 < result <= 1000 else None
    except (ValueError, ZeroDivisionError, TypeError, OverflowError):
        return None


def caption_band(frames):
    """Look for persistent high-contrast edges in a changing lower-third band.

    This is not OCR and cannot establish whether text is actually a caption.
    """
    if len(frames) < 3: return False
    grays=[cv2.cvtColor(cv2.resize(f,(320,180)),cv2.COLOR_BGR2GRAY) for f in frames]
    changes=[np.mean(cv2.absdiff(a[:110],b[:110])) for a,b in zip(grays,grays[1:])]
    if np.mean(changes) < 4: return False
    edges=np.stack([cv2.Canny(g,80,180)>0 for g in grays])
    for y in range(120,170,5):
        band=edges[:,y:y+10,16:304]
        dense=band.mean(axis=(1,2)) >= .045
        # Persistent pixel positions reduce false positives from moving texture.
        stable=(band.mean(axis=0) >= .6).mean()
        if dense.mean() >= .6 and stable >= .025: return True
    return False


def inspect(details, frames, face_counts):
    w,h=details.get('width'),details.get('height')
    if not isinstance(w,int) or not isinstance(h,int) or w<=0 or h<=0: raise ValueError('Invalid source dimensions.')
    try: duration=float(details.get('duration') or 0)
    except (TypeError,ValueError): duration=0
    if not math.isfinite(duration) or duration <= 0: raise ValueError('Invalid source duration.')
    fps=frame_rate(details.get('frame_rate')); nominal=frame_rate(details.get('nominal_frame_rate'))
    upscale=shorts.estimate_upscale(w,h,reframe.ReframeOptions())
    warnings=[]
    def warn(code,severity,message,detail): warnings.append(dict(code=code,severity=severity,message=message,detail=detail))
    if w/h <= 9/16: warn('already_vertical','info','Source is already vertical; consider captions only.','Letterbox is unnecessary for an exact 9:16 source; narrower sources retain their full frame with side bars.')
    if min(w,h)<720 or upscale>1.8: warn('low_resolution','warn','This source may look soft when enlarged.',f'Short side: {min(w,h)} px; follow upscale: {upscale:.2f}×.')
    if duration<90: warn('too_short','info','Source is under 90 seconds.','Fewer candidate clips may be available.')
    if not details.get('has_audio'): warn('no_audio','block','No audio stream found.','Captions and AI selection require speech audio or an existing transcript.')
    if caption_band(frames): warn('possible_burned_in_captions','warn','Possible burned-in captions detected.','Check the source; adding captions could overlap existing text.')
    if not any(face_counts): warn('no_faces_detected','info','No faces detected in sampled frames.','Face detection is approximate; subjects may be missed.')
    cuts=sum(cv2.compareHist(reframe._hist(a),reframe._hist(b),cv2.HISTCMP_BHATTACHARYYA)>.55 for a,b in zip(frames,frames[1:]))
    rate=cuts*60/duration
    if cuts>=3 and rate>=10: warn('high_cut_rate','info','Many changes between sampled frames.','This is a coarse estimate, not an exact cut count.')
    if fps and nominal and abs(fps-nominal)/nominal > .02: warn('variable_frame_rate','info','Source may use a variable frame rate.','Average and nominal frame rates differ; this is only an indication.')
    if not fps or min(abs(fps-x) for x in (24,25,30,50,60,23.976,29.97,59.94))>.15: warn('odd_frame_rate','info','Source frame rate is unusual or unavailable.','The export may use a different frame rate.')
    return {'ok':not any(x['severity']=='block' for x in warnings),'warnings':warnings,'stats':{'width':w,'height':h,'duration':duration,'frame_rate':fps,'nominal_frame_rate':nominal,'sampled_frames':len(frames),'faces_detected':sum(face_counts),'sampled_changes':cuts,'estimated_follow_upscale':round(upscale,3)}}


def run(path, check_cancel=lambda: None):
    details=media.probe_video(path)
    try: duration=float(details.get('duration') or 0)
    except (TypeError,ValueError): duration=0
    if not math.isfinite(duration) or duration<=0: raise ValueError('Invalid source duration.')
    cap=cv2.VideoCapture(str(path))
    if not cap.isOpened(): raise ValueError('Could not sample the source video.')
    frames=[]; counts=[]
    try:
        detector=reframe.FaceDetector(allow_download=False)
        for i in range(12):
            check_cancel()
            cap.set(cv2.CAP_PROP_POS_MSEC,1000*duration*(i+.5)/12)
            ok,frame=cap.read()
            if ok and frame is not None and frame.size:
                # Bound face detection and frame analysis memory independently of source dimensions.
                h,w=frame.shape[:2]
                scale=min(1.,640/max(w,h))
                if scale<1: frame=cv2.resize(frame,(max(1,round(w*scale)),max(1,round(h*scale))),interpolation=cv2.INTER_AREA)
                frames.append(frame); counts.append(len(detector.detect(frame)))
    finally: cap.release()
    if not frames: raise ValueError('Could not sample any source frames.')
    return inspect(details,frames,counts)


def failed():
    return {'ok':True,'warnings':[{'code':'preflight_failed','severity':'info','message':'Source preflight could not be completed.','detail':'You can continue; review the source manually.'}],'stats':{}}
