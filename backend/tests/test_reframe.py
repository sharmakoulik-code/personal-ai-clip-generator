from app.services import reframe
from app.services.reframe import ReframeOptions, Sample


def test_dead_zone_prevents_jitter():
    opts = ReframeOptions()
    t = [i / 6 for i in range(30)]
    jitter = [500 + (8 if i % 2 else -8) for i in range(30)]
    cam = reframe.smooth_scene(t, jitter, 600, opts, 30)
    assert max(cam) - min(cam) < 1.0


def test_camera_follows_big_move_without_overshoot_or_speed_violation():
    opts = ReframeOptions()
    t = [i / 6 for i in range(60)]
    cam = reframe.smooth_scene(t, [400] * 20 + [1200] * 40, 600, opts, 30)
    speeds = [abs(b - a) * 30 for a, b in zip(cam, cam[1:])]
    assert max(speeds) <= opts.max_speed * 600 + 1e-6
    assert max(cam) <= 1200 + 1e-6
    assert abs(cam[-1] - 1200) < 150


def test_path_covers_every_frame_and_resets_on_cut():
    opts = ReframeOptions()
    samples = [Sample(i / 6, 300, 360, 100, False, 'face') for i in range(12)]
    samples += [Sample(2 + i / 6, 1000, 360, 100, i == 0, 'face') for i in range(12)]
    w, h, path = reframe.build_camera_path(samples, 1280, 720, 30, opts, 120)
    assert len(path) == 120
    assert all(0 <= x <= 1280 - w + 1e-6 for _, x, _ in path)


def test_lost_subject_holds_then_stays_put():
    opts = ReframeOptions()
    samples = [Sample(i / 6, 900, 360, 100, False, 'face') for i in range(6)] + [Sample(1 + i / 6) for i in range(30)]
    _, _, path = reframe.build_camera_path(samples, 1280, 720, 30, opts, 180)
    xs = [x for _, x, _ in path]
    assert max(xs) - min(xs) < 5


def test_subject_tracking_real_video(tmp_path, monkeypatch):
    import os, shutil, subprocess
    from pathlib import Path
    import cv2
    import numpy as np
    import pytest
    from app.services import media, rendering
    ffmpeg = os.environ.get('CLIP_FFMPEG') or shutil.which('ffmpeg')
    ffprobe = os.environ.get('CLIP_FFPROBE') or shutil.which('ffprobe')
    if not ffmpeg or not ffprobe: pytest.skip('FFmpeg and FFprobe required')
    monkeypatch.setenv('CLIP_STORAGE_PATH', str(tmp_path / 'storage'))
    monkeypatch.setenv('CLIP_NO_MODEL_DOWNLOAD', '1')
    portrait = cv2.imread(str(Path(__file__).parent / 'fixtures' / 'astronaut.jpg'))
    detector = reframe.FaceDetector(allow_download=False)
    faces = detector.detect(portrait)
    assert faces
    face = max(faces, key=lambda f: f[2]*f[3])
    image = cv2.resize(portrait, (260,260))
    source = tmp_path/'motion.avi'
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*'MJPG'), 10, (1280,720))
    assert writer.isOpened()
    for i in range(50):
        frame = np.full((720,1280,3), 90, np.uint8)
        x = 130 + i*14
        frame[100:360,x:x+260] = image
        writer.write(frame)
    writer.release()
    video = tmp_path/'source.mp4'; out = tmp_path/'out.mp4'
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-i',str(source),'-f','lavfi','-i','sine=frequency=440','-t','5','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-y',str(video)],check=True)
    opts = ReframeOptions()
    samples,(w,h,fps)=reframe.analyze_subject(video,0,5,opts,detector)
    cw,ch,path=reframe.build_camera_path(samples,w,h,fps,opts,50)
    assert sum(s.source == 'face' for s in samples) >= len(samples)*.5
    fx,fy,fw,fh,_=face
    visible = sum(x0 <= 130+i*14+fx*260/512 and x0+cw >= 130+i*14+(fx+fw)*260/512 and y0 <= 100+fy*260/512 and y0+ch >= 100+(fy+fh)*260/512 for i,(_,x0,y0) in enumerate(path))
    assert visible >= len(path)*.9
    settings = {'layout':'smart','zoom':1,'horizontal':0,'vertical':0,'tracking_engine':'subject'}
    points,status = rendering.subject_track(video,0,5,settings,lambda:None)
    assert status=='haar_face_framing'
    graph=rendering.filter_graph({'width':w,'height':h},settings,points,1080,1920)
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-i',str(video),'-filter_complex',graph,'-map','[v]','-map','0:a','-c:v','libx264','-preset','ultrafast','-c:a','aac','-y',str(out)],check=True)
    info=media.probe_video(out)
    assert (info['width'],info['height'])==(1080,1920) and info['audio_codec']=='aac'


def test_assign_tracks_expiration_and_cuts():
    def face(x, size=100): return (x, 10, size, size, .9, None, 0.)
    samples=[Sample(0,faces=[face(100),face(700)]), Sample(.5,faces=[face(120),face(690)]), Sample(1.6,faces=[face(120)]), Sample(1.7,cut=True,faces=[face(120)])]
    assert reframe.assign_tracks(samples,1000)==4
    assert [f[5] for f in samples[0].faces]==[f[5] for f in samples[1].faces]
    assert samples[2].faces[0][5] != samples[1].faces[0][5]
    assert samples[3].faces[0][5] != samples[2].faces[0][5]


def test_speech_intervals_merge_and_discard_zero_duration():
    words=[{'start':2,'end':2.2},{'start':1,'end':1.8},{'start':3,'end':3},{'start':4,'end':5}]
    assert reframe.speech_intervals(words)==[(1.,2.2),(4.,5.)]


def test_speaker_hysteresis_silence_group_and_single_face():
    opts=ReframeOptions()
    def face(x,tid,act): return (x,100,100,100,.9,tid,act)
    samples=[Sample(i*.1,faces=[face(100,0,.1 if i<25 else .01),face(700,1,.01 if i<25 else .4)]) for i in range(60)]
    switches=reframe.choose_speaker(samples,[(0,5)],opts,300)
    assert switches == 1
    assert samples[15].speaker_id==0 and samples[45].speaker_id==1
    assert all(s.speaker_id==1 for s in samples[51:])
    one=Sample(0,110,150,100,False,'face',faces=[face(100,0,.9)])
    group=Sample(.1,faces=[face(100,0,.1),face(250,1,.2)])
    reframe.choose_speaker([one,group],[(0,1)],opts,300)
    assert one.cx==110 and one.source=='face'
    assert group.source=='group' and group.cx==225
    short=[Sample(i*.1,faces=[face(100,0,.2),face(700,1,.01 if i<20 else .8 if i<25 else .01)]) for i in range(40)]
    assert reframe.choose_speaker(short,[(0,4)],opts,300)==0


def test_two_faces_mouth_motion_in_synthetic_video(tmp_path):
    import cv2, numpy as np
    from pathlib import Path
    portrait=cv2.imread(str(Path(__file__).parent/'fixtures'/'astronaut.jpg'))
    # Two copies provide textured faces; only the right mouth region changes.
    face=cv2.resize(portrait[40:230,140:330],(150,150))
    source=tmp_path/'two.avi'
    writer=cv2.VideoWriter(str(source),cv2.VideoWriter_fourcc(*'MJPG'),10,(960,540))
    assert writer.isOpened()
    for i in range(100):
        frame=np.full((540,960,3),90,np.uint8)
        offset=i//2
        frame[140:290,80+offset:230+offset]=face; frame[140:290,710+offset:860+offset]=face
        frame[230:290,740+offset:830+offset]=20 if i%2 else 230
        writer.write(frame)
    writer.release()
    class KnownBoxes:
        def __init__(self): self.index=0
        def detect(self, frame):
            offset=self.index//2; self.index+=1
            return [(80+offset,140,150,150,.9),(710+offset,140,150,150,.9)]
    samples,(w,h,fps)=reframe.analyze_subject(source,0,10,ReframeOptions(),KnownBoxes(),speech=[(0,10)])
    selected=[s for s in samples if s.t>=2 and s.source=='speaker_heuristic']
    assert len(selected)>20 and sum(s.cx>500 for s in selected)>=len(selected)*.8
    cw,ch,path=reframe.build_camera_path(samples,w,h,fps,ReframeOptions(),100)
    assert sum(x+cw/2>500 for t,x,y in path if t>=2) >= 50


def test_scene_cut_camera_samples_preserve_both_sides(tmp_path, monkeypatch):
    from app.services import rendering
    class Detector:
        kind='fake'
        def __init__(self,*args,**kwargs): pass
    monkeypatch.setattr(reframe,'FaceDetector',Detector)
    samples=[Sample(i/10,200,360,100,False,'face',faces=[(150,310,100,100,.9,None,0)]) for i in range(20)]
    samples += [Sample(2+i/10,1000,360,100,i==0,'face',faces=[(950,310,100,100,.9,None,0)]) for i in range(20)]
    monkeypatch.setattr(reframe,'analyze_subject',lambda *args,**kwargs:(samples,(1280,720,10)))
    settings={'zoom':1,'tracking_engine':'subject'}
    points,_=rendering.subject_track(tmp_path/'unused.mp4',0,4,settings,lambda:None)
    before=next(p for p in points if p['t']==1.9); after=next(p for p in points if p['t']==2)
    assert after['x']-before['x']>.4


def test_cut_without_faces_centers_in_new_scene():
    samples=[Sample(0,1000,300,100), Sample(1,cut=True), Sample(2)]
    xs,ys,_=reframe._fill_gaps(samples,ReframeOptions(),640,360)
    assert xs==[1000,640,640] and ys==[300,360,360]
