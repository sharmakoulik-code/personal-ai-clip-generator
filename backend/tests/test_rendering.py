import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from app import database
from app.main import app
from app.services import framing, rendering, media


def wait(client, rid, limit=90):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        job = client.get('/api/renders/' + rid).json()
        if job['status'] in ('completed', 'failed', 'cancelled'): return job
        time.sleep(.1)
    pytest.fail(f'Render timeout: {job}')


@pytest.fixture
def app_with_video(tmp_path, monkeypatch):
    ffmpeg = os.environ.get('CLIP_FFMPEG') or shutil.which('ffmpeg')
    ffprobe = os.environ.get('CLIP_FFPROBE') or shutil.which('ffprobe')
    if not ffmpeg or not ffprobe: pytest.skip('FFmpeg and FFprobe required')
    monkeypatch.setenv('CLIP_DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('CLIP_STORAGE_PATH', str(tmp_path / 'storage'))
    monkeypatch.delenv('CLIP_DISABLE_WORKER', raising=False)
    source = tmp_path / 'source.mp4'
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=320x180:rate=10','-f','lavfi','-i','sine=frequency=440','-t','2','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-y',str(source)], check=True)
    with TestClient(app) as client:
        project = client.post('/api/projects', json={'name':'Render test'}).json()['id']
        data = source.read_bytes()
        upload = client.post('/api/videos/upload', json={'project_id':project,'filename':'source.mp4','size':len(data)}).json()
        assert client.put(f"/api/jobs/{upload['id']}/upload", content=data).status_code == 202
        for i in range(100):
            result = client.get(f"/api/jobs/{upload['id']}").json()
            if result['status'] in ('failed','completed'): break
            time.sleep(.05)
        assert result['status'] == 'completed', result
        vid = result['video_id']
        with database.connect() as db:
            for ident, start, end in [('clip1', 0, 1.5), ('clip2', .5, 1.8)]:
                db.execute('''INSERT INTO candidate_clips(id,video_id,start,end,title,hook,transcript,reason,score,scores_json,selected) VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (ident,vid,start,end,ident,'hello','hello','fixture',50,'{}',1))
        yield client, project, vid, tmp_path


@pytest.mark.parametrize('layout,resolution', [('center',1280), ('smart',1280), ('blur',1280), ('split',1280), ('center',1920)])
def test_real_ffmpeg_render_layouts(app_with_video, layout, resolution):
    client, project, vid, tmp_path = app_with_video
    preview = client.get('/api/clips/clip1/render-preview', params={'layout':layout,'resolution':resolution})
    assert preview.status_code == 200, preview.text
    assert preview.headers['content-type'] == 'image/jpeg'
    response = client.post('/api/clips/clip1/render', json={'layout':layout,'resolution':resolution,'quality':'small'})
    assert response.status_code == 202, response.text
    result = wait(client,response.json()['id'])
    assert result['status'] == 'completed', result
    assert (result['width'], result['height']) == (resolution*9//16, resolution)
    output = client.get(f"/api/renders/{result['id']}/download")
    assert output.status_code == 200 and output.content
    dest = tmp_path / f'{layout}-{resolution}.mp4'; dest.write_bytes(output.content)
    info = media.probe_video(dest)
    assert (info['width'],info['height']) == (resolution*9//16,resolution)
    assert info['has_audio'] and abs(info['duration'] - 1.5) < .35
    assert client.get(f"/api/renders/{result['id']}/stream", headers={'Range':'bytes=0-99'}).status_code == 206
    subprocess.run([os.environ['CLIP_FFMPEG'],'-hide_banner','-loglevel','error','-i',str(dest),'-f','null','-'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    assert client.delete(f"/api/renders/{result['id']}").status_code == 204


def test_frame_boundaries_and_no_speaker_claim():
    assert framing.target([], 1000, 600)[2] == 'no_face_center'
    assert framing.target([(100,100,80,80),(200,100,80,80)],1000,600)[2] == 'group'
    assert framing.target([(0,0,50,50),(900,0,80,80)],1000,600)[2] == 'largest_face'
    previous=(500.,300.); current=framing.smooth(previous,(900.,0.),1,1000,600)
    assert abs(current[0]-previous[0]) <= 120 and abs(current[1]-previous[1]) <= 72
    cw,ch,pts=framing.crop_positions([{'t':0,'x':0,'y':1}],1000,600,1.2,-1,1)
    assert 0 <= pts[0]['x'] <= 1000-cw and 0 <= pts[0]['y'] <= 600-ch


def test_batch_validation_and_cancellation(app_with_video, monkeypatch):
    client, project, vid, tmp = app_with_video
    rendering.stop_worker()
    response = client.post(f'/api/projects/{project}/render-batch',json={'layout':'center','resolution':1280})
    assert response.status_code == 202 and len(response.json()) == 2
    ids = [r['id'] for r in response.json()]
    assert len({r['batch_id'] for r in response.json()}) == 1
    assert client.post('/api/clips/clip1/render',json={}).status_code == 409
    assert client.delete('/api/videos/'+vid).status_code == 409
    assert client.post('/api/clips/clip1/render',json={'zoom':100}).status_code == 422
    assert client.post('/api/renders/'+ids[0]+'/cancel').json()['status'] == 'cancelled'
    assert client.post('/api/renders/'+ids[0]+'/retry').status_code == 202
    rendering.start_worker()


def test_real_face_detection_if_fixture_available():
    face = os.environ.get('CLIP_TEST_FACE_IMAGE')
    ffmpeg = os.environ.get('CLIP_FFMPEG')
    if not face or not ffmpeg: pytest.skip('Set CLIP_TEST_FACE_IMAGE and CLIP_FFMPEG')
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        video = Path(d) / 'face.mp4'
        subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-loop','1','-i',face,'-t','2','-r','10','-c:v','libx264','-pix_fmt','yuv420p','-y',str(video)],check=True)
        points,status=framing.detect_frames(video,0,1.8,.5)
        assert status == 'face_framing' and len(points) >= 3


def test_actual_speech_through_ingestion_analysis_and_render(tmp_path, monkeypatch):
    model = os.environ.get('CLIP_TEST_MODEL_DIR')
    source = os.environ.get('CLIP_TEST_SPEECH_VIDEO')
    if not model or not source: pytest.skip('Set CLIP_TEST_MODEL_DIR and CLIP_TEST_SPEECH_VIDEO')
    monkeypatch.setenv('CLIP_DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('CLIP_STORAGE_PATH', str(tmp_path / 'storage'))
    monkeypatch.delenv('CLIP_DISABLE_WORKER', raising=False)
    shutil.copytree(model, tmp_path / 'storage' / 'models' / 'whisper-base')
    from app.services import analysis
    data=Path(source).read_bytes()
    with TestClient(app) as client:
        project=client.post('/api/projects',json={'name':'Speech to render'}).json()['id']
        upload=client.post('/api/videos/upload',json={'project_id':project,'filename':'speech.mp4','size':len(data)}).json()
        assert client.put(f"/api/jobs/{upload['id']}/upload",content=data).status_code == 202
        for _ in range(200):
            imported=client.get(f"/api/jobs/{upload['id']}").json()
            if imported['status'] in ('completed','failed'): break
            time.sleep(.05)
        assert imported['status']=='completed',imported
        video_id=imported['video_id']
        job=client.post(f'/api/videos/{video_id}/analyze',json={'model':'base','duration_range':'15-30'}).json()
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            result=client.get(f"/api/analysis/jobs/{job['id']}").json()
            if result['status'] in ('completed','failed','cancelled'): break
            time.sleep(.2)
        assert result['status']=='completed',result
        transcript=client.get(f'/api/videos/{video_id}/transcription').json()
        assert 'ask not' in ' '.join(s['text'] for s in transcript['segments']).lower()
        clips=client.get(f'/api/videos/{video_id}/clips').json(); assert clips
        clip=clips[0]
        assert client.patch('/api/clips/'+clip['id'],json={'selected':True}).json()['selected']
        render=client.post(f'/api/projects/{project}/render-batch',json={'layout':'center','resolution':1280,'quality':'small'}).json()[0]
        done=wait(client,render['id'],180); assert done['status']=='completed',done
        out=tmp_path / 'speech-output.mp4'; out.write_bytes(client.get(f"/api/renders/{render['id']}/download").content)
        info=media.probe_video(out)
        assert (info['width'],info['height'],info['video_codec'],info['audio_codec'])==(720,1280,'h264','aac')
        assert abs(info['duration']-(clip['end']-clip['start'])) < .5
        subprocess.run([os.environ['CLIP_FFMPEG'],'-hide_banner','-loglevel','error','-i',str(out),'-map','0:v:0','-map','0:a:0','-f','null','-'],check=True,stderr=subprocess.PIPE)
        print('REAL SPEECH RENDER:',info,'CLIP:',clip['start'],clip['end'])


def test_moving_real_face_tracking_and_crop(tmp_path):
    face = os.environ.get('CLIP_TEST_FACE_IMAGE'); ffmpeg = os.environ.get('CLIP_FFMPEG')
    if not face or not ffmpeg: pytest.skip('Set CLIP_TEST_FACE_IMAGE and CLIP_FFMPEG')
    source = tmp_path / 'moving.mp4'
    args = [ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i','color=black:size=960x540:rate=10',
            '-loop','1','-i',face,'-f','lavfi','-i','sine=frequency=440',
            '-filter_complex',"[1:v]scale=220:220[f];[0:v][f]overlay=x='80+100*t':y=100:shortest=1[v]",
            '-map','[v]','-map','2:a','-t','3','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-y',str(source)]
    subprocess.run(args,check=True,stderr=subprocess.PIPE)
    points,status=framing.detect_frames(source,0,2.8,.5)
    assert status=='face_framing',status
    assert points[-1]['x'] > points[0]['x'] + .03, points
    for a,b in zip(points,points[1:]):
        assert abs(b['x']-a['x']) <= .061
    settings={'layout':'smart','zoom':1.0,'horizontal':0.0,'vertical':0.0}
    graph=rendering.filter_graph({'width':960,'height':540},settings,points,720,1280)
    output=tmp_path / 'tracked.mp4'
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-i',str(source),'-filter_complex',graph,'-map','[v]','-map','0:a','-c:v','libx264','-preset','ultrafast','-c:a','aac','-y',str(output)],check=True,stderr=subprocess.PIPE)
    info=media.probe_video(output)
    assert (info['width'],info['height'])==(720,1280) and info['has_audio']


def test_render_restart_marks_interrupted_and_resumes_pending(app_with_video):
    client, project, vid, tmp = app_with_video
    rendering.stop_worker()
    rows=client.post(f'/api/projects/{project}/render-batch',json={'layout':'center','resolution':1280,'quality':'small'}).json()
    with database.connect() as db:
        db.execute("UPDATE renders SET status='running',stage='Rendering video' WHERE id=?",(rows[0]['id'],))
    rendering.start_worker()
    interrupted=client.get('/api/renders/'+rows[0]['id']).json()
    assert interrupted['status']=='failed' and interrupted['stage']=='Interrupted'
    resumed=wait(client,rows[1]['id']); assert resumed['status']=='completed',resumed
    assert client.get(f'/api/renders?video_id={vid}').json()[0]['status'] in ('completed','failed')


def test_active_render_cancel_and_retry(app_with_video, monkeypatch):
    import threading
    client, project, vid, tmp = app_with_video
    entered, release = threading.Event(), threading.Event()
    original=rendering.run_ffmpeg
    def blocked(args,rid,duration):
        entered.set(); release.wait(5); rendering.check_cancel(rid)
    monkeypatch.setattr(rendering,'run_ffmpeg',blocked)
    job=client.post('/api/clips/clip1/render',json={'layout':'center','resolution':1280}).json()
    assert entered.wait(5)
    assert client.delete(f'/api/projects/{project}').status_code == 409
    assert client.delete('/api/renders/'+job['id']).status_code == 409
    assert client.post('/api/renders/'+job['id']+'/cancel').status_code == 200
    release.set()
    assert wait(client,job['id'])['status']=='cancelled'
    monkeypatch.setattr(rendering,'run_ffmpeg',original)
    retry=client.post('/api/renders/'+job['id']+'/retry')
    assert retry.status_code==202
    result=wait(client,retry.json()['id']); assert result['status']=='completed',result


def test_face_cache_reuse(app_with_video, monkeypatch):
    client, project, vid, tmp = app_with_video
    video, path=rendering.source_video(vid)
    calls=[]
    def detect(*args):
        calls.append(1)
        return [{'t':0,'x':.5,'y':.5}],'no_face_center'
    monkeypatch.setattr(framing,'detect_frames',detect)
    settings={'layout':'smart','track_faces':True,'detect_interval':1}
    a=rendering.track(video,path,0,1,settings,lambda:None,lambda p:None)
    b=rendering.track(video,path,0,1,settings,lambda:None,lambda p:None)
    assert a==b and calls==[1]


@pytest.mark.parametrize('size', ['320x180', '180x320', '240x240'])
def test_source_aspect_ratios_render_correctly(tmp_path, size):
    ffmpeg=os.environ.get('CLIP_FFMPEG')
    if not ffmpeg or not os.environ.get('CLIP_FFPROBE'): pytest.skip('FFmpeg and FFprobe required')
    width,height=(int(x) for x in size.split('x'))
    source=tmp_path / 'source.mp4'; out=tmp_path / 'out.mp4'
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i',f'testsrc=size={size}:rate=5','-t','1','-pix_fmt','yuv420p','-c:v','libx264','-y',str(source)],check=True)
    graph=rendering.filter_graph({'width':width,'height':height},{'layout':'center','zoom':1,'horizontal':.25,'vertical':-.25},[{'t':0,'x':.5,'y':.5}],720,1280)
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-i',str(source),'-filter_complex',graph,'-map','[v]','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-y',str(out)],check=True)
    metadata=media.probe_video(out)
    assert (metadata['width'],metadata['height']) == (720,1280)


def test_optional_gpu_encoder_configuration(monkeypatch):
    monkeypatch.delenv('CLIP_RENDER_ENCODER',raising=False)
    assert rendering.encoder_args('balanced')[:2] == ['-c:v','libx264']
    monkeypatch.setenv('CLIP_RENDER_ENCODER','nvenc')
    assert rendering.encoder_args('balanced')[:2] == ['-c:v','h264_nvenc']
    monkeypatch.setenv('CLIP_RENDER_ENCODER','invalid')
    with pytest.raises(ValueError,match='cpu or nvenc'): rendering.encoder_args('balanced')


def test_analysis_rejected_while_render_active(app_with_video):
    client, project, vid, tmp = app_with_video
    rendering.stop_worker()
    assert client.post('/api/clips/clip1/render',json={'layout':'center'}).status_code==202
    response=client.post(f'/api/videos/{vid}/analyze',json={})
    assert response.status_code==409 and 'renders' in response.json()['detail']
    rendering.start_worker()


def test_rotation_metadata_and_render(tmp_path):
    ffmpeg=os.environ.get('CLIP_FFMPEG')
    if not ffmpeg or not os.environ.get('CLIP_FFPROBE'): pytest.skip('FFmpeg and FFprobe required')
    original=tmp_path/'original.mp4'; source=tmp_path/'rotated.mp4'; out=tmp_path/'out.mp4'
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=320x180:rate=5','-t','1','-c:v','libx264','-pix_fmt','yuv420p','-y',str(original)],check=True)
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-i',str(original),'-c','copy','-metadata:s:v:0','rotate=90','-y',str(source)],check=True)
    info=media.probe_video(source); assert (info['width'],info['height'])==(180,320)
    graph=rendering.filter_graph(info,{'layout':'center','zoom':1,'horizontal':0,'vertical':0},[{'t':0,'x':.5,'y':.5}],720,1280)
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-i',str(source),'-filter_complex',graph,'-map','[v]','-c:v','libx264','-preset','ultrafast','-y',str(out)],check=True)
    actual=media.probe_video(out); assert (actual['width'],actual['height'])==(720,1280)


def test_invalid_preview_settings_and_failure_retry(app_with_video, monkeypatch):
    client, project, vid, tmp = app_with_video
    assert client.get('/api/clips/clip1/render-preview?resolution=1300').status_code == 422
    assert client.post('/api/clips/clip1/render',json={'resolution':1300}).status_code == 422
    original=rendering.run_ffmpeg
    def fail(*args): raise ValueError('Simulated encoder failure')
    monkeypatch.setattr(rendering,'run_ffmpeg',fail)
    job=client.post('/api/clips/clip1/render',json={'layout':'center','resolution':1280}).json()
    failed=wait(client,job['id']); assert failed['status']=='failed' and 'Simulated' in failed['error']
    assert not (tmp/'storage'/'rendered'/str(project)/(job['id']+'.mp4')).exists()
    monkeypatch.setattr(rendering,'run_ffmpeg',original)
    retry=client.post('/api/renders/'+job['id']+'/retry').json()
    assert wait(client,retry['id'])['status']=='completed'


def test_deleting_video_removes_completed_output_and_cache(app_with_video):
    client, project, vid, tmp = app_with_video
    job=client.post('/api/clips/clip1/render',json={'layout':'smart','resolution':1280,'quality':'small'}).json()
    result=wait(client,job['id']); assert result['status']=='completed',result
    path=tmp/'storage'/'rendered'/str(project)/(job['id']+'.mp4'); assert path.exists()
    with database.connect() as db: assert db.execute('select count(*) from face_track_cache where video_id=?',(vid,)).fetchone()[0]
    assert client.delete('/api/videos/'+vid).status_code==204
    assert not path.exists() and client.get('/api/renders/'+job['id']).status_code==404
    with database.connect() as db: assert not db.execute('select count(*) from face_track_cache where video_id=?',(vid,)).fetchone()[0]


def test_subject_render_request_validation(app_with_video):
    client, project, vid, tmp = app_with_video
    assert client.post('/api/clips/missing/render', json={'tracking_engine':'subject'}).status_code == 404
    assert client.post('/api/clips/clip1/render', json={'tracking_engine':'subject','zoom':5}).status_code == 422
    assert client.get('/api/renders/missing').status_code == 404
    job=client.post('/api/clips/clip1/render', json={'tracking_engine':'subject','layout':'smart','resolution':1280,'quality':'small'}).json()
    result=wait(client,job['id'])
    assert result['status']=='completed' and result['tracking_status'].startswith('haar_'), result


def test_caption_srt_and_real_burn_in(app_with_video):
    import json
    client, project, vid, tmp = app_with_video
    with database.connect() as db:
        db.execute("INSERT INTO transcripts(video_id,model,language,duration) VALUES(?,?,?,?)",(vid,'base','en',2))
        words=[{'start':.1,'end':.5,'word':'hello'},{'start':.6,'end':1.2,'word':'world'}]
        db.execute("INSERT INTO transcript_segments(video_id,position,start,end,text,words_json) VALUES(?,?,?,?,?,?)",(vid,0,.1,1.2,'hello world',json.dumps(words)))
    srt=client.get('/api/clips/clip1/captions.srt')
    assert srt.status_code == 200 and '00:00:00,100' in srt.text and 'hello world' in srt.text
    ffmpeg=os.environ.get('CLIP_FFMPEG') or shutil.which('ffmpeg')
    filters=subprocess.run([ffmpeg,'-hide_banner','-filters'],capture_output=True,text=True,check=True).stdout
    if not any(' ass ' in line for line in filters.splitlines()): pytest.skip('FFmpeg libass filter unavailable')
    response=client.post('/api/clips/clip1/render',json={'layout':'center','resolution':1280,'quality':'small','captions':True,'caption_style':'highlight'})
    job=wait(client,response.json()['id']); assert job['status']=='completed',job
    out=tmp/'caption.mp4'; out.write_bytes(client.get(f"/api/renders/{job['id']}/download").content)
    assert media.probe_video(out)['video_codec']=='h264'


def test_missing_caption_words_fails_explicitly(app_with_video):
    client, _, _, _=app_with_video
    assert client.get('/api/clips/clip1/captions.srt').status_code==422
    job=client.post('/api/clips/clip1/render',json={'layout':'center','resolution':1280,'captions':True}).json()
    done=wait(client,job['id']); assert done['status']=='failed' and 'No timed transcript words' in done['error']


def test_padding_and_loudness_are_opt_in(app_with_video):
    client, project, vid, tmp = app_with_video
    job=client.post('/api/clips/clip2/render',json={'layout':'center','resolution':1280,'quality':'small','pad':.2,'normalize_audio':True}).json()
    assert abs(job['start']-.3)<.001 and abs(job['end']-2)<.001
    result=wait(client,job['id']); assert result['status']=='completed',result
    out=tmp/'normalized.mp4'; out.write_bytes(client.get(f"/api/renders/{job['id']}/download").content)
    assert abs(media.probe_video(out)['duration']-1.7)<.35


def test_active_speaker_reports_missing_transcript(app_with_video, monkeypatch):
    monkeypatch.setenv('CLIP_NO_MODEL_DOWNLOAD','1')
    client, _, _, _=app_with_video
    job=client.post('/api/clips/clip1/render',json={'layout':'smart','tracking_engine':'subject','active_speaker':True,'resolution':1280,'quality':'small'}).json()
    result=wait(client,job['id']); assert result['status']=='completed',result
    assert result['info']['speaker_status']=='no_transcript'


@pytest.mark.parametrize('layout,expected', [('auto','fit_blur'), ('follow','follow'), ('fit_blur','fit_blur'), ('stack','fit_blur')])
def test_shorts_layouts_real_render_and_preview(app_with_video, layout, expected):
    client, _, _, tmp = app_with_video
    settings={'layout':layout,'resolution':1280,'quality':'small'}
    preview=client.get('/api/clips/clip1/render-preview',params=settings)
    assert preview.status_code==200, preview.text
    response=client.post('/api/clips/clip1/render',json=settings)
    assert response.status_code==202, response.text
    job=wait(client,response.json()['id']); assert job['status']=='completed',job
    assert job['info']['layout_used']==expected
    assert job['info']['upscale_factor'] > 1.5
    if layout=='stack': assert job['info']['layout_fallback_reason']
    output=tmp/'short.mp4'; output.write_bytes(client.get(f"/api/renders/{job['id']}/download").content)
    meta=media.probe_video(output)
    assert (meta['width'],meta['height'])==(720,1280) and meta['has_audio']


def test_stack_two_persistent_tracks_real_render(app_with_video, monkeypatch):
    from app.services import reframe
    client, _, _, _ = app_with_video
    def sample(path,start,end,opts,detector,check,speech=None):
        return [reframe.Sample(start+i/10, faces=[(30.,30.,40.,40.,.9,0,0.), (220.,30.,40.,40.,.9,1,0.)]) for i in range(15)], (320,180,10.)
    monkeypatch.setattr(reframe,'analyze_subject',sample)
    settings={'layout':'stack','resolution':1280,'quality':'small'}
    preview=client.get('/api/clips/clip1/render-preview',params=settings)
    assert preview.status_code==200,preview.text
    response=client.post('/api/clips/clip1/render',json=settings)
    assert response.status_code==202
    job=wait(client,response.json()['id']); assert job['status']=='completed',job
    assert job['info']['layout_used']=='stack' and job['info']['tracks']==2


def test_shorts_layout_choice_and_sharpen():
    from app.services import reframe, shorts
    opts=reframe.ReframeOptions()
    assert shorts.estimate_upscale(1920,1080,opts)==pytest.approx(1920/1080)
    assert shorts.estimate_upscale(1080,1920,opts)==1
    assert shorts.choose_layout('auto',1.2)==('follow',None)
    assert shorts.choose_layout('auto',2)==('fit_blur',None)
    assert shorts.choose_layout('auto',2,True,True)==('stack',None)
    assert shorts.choose_layout('stack',1,False,False)[0]=='fit_blur'
    pts=[{'t':0,'x':.5,'y':.5}]
    cfg={'layout':'follow','zoom':1,'horizontal':0,'vertical':0,'sharpen':.35}
    assert 'unsharp' in rendering.filter_graph({'width':320,'height':180},cfg,pts,720,1280)
    assert 'unsharp' not in rendering.filter_graph({'width':1080,'height':1920},cfg,pts,1080,1920)


def test_shorts_threshold_boundaries_and_track_ranking():
    from app.services import reframe, shorts
    assert shorts.choose_layout('auto', 1.5) == ('follow', None)
    assert shorts.choose_layout('auto', 1.50001) == ('fit_blur', None)
    opts=reframe.ReframeOptions()
    assert shorts.estimate_upscale(1920,1080,opts)==pytest.approx(1920/1080)
    assert shorts.estimate_upscale(3840,2160,opts)==pytest.approx(1920/2160)
    # The two largest area-time tracks must themselves pass visibility.
    samples=[]
    for i in range(10):
        faces=[(10,0,30,30,1,0,0),(60,0,10,10,1,2,0)]
        if i < 5: faces.append((110,0,50,50,1,1,0))
        samples.append(reframe.Sample(i/10,faces=faces))
    assert reframe.prominent_tracks(samples,1)==[]


def test_one_face_stack_falls_back(app_with_video, monkeypatch):
    from app.services import reframe
    client, _, _, _ = app_with_video
    def sample(path,start,end,opts,detector,check,speech=None):
        return [reframe.Sample(start+i/10, faces=[(30.,30.,40.,40.,.9,0,0.)]) for i in range(15)], (320,180,10.)
    monkeypatch.setattr(reframe,'analyze_subject',sample)
    response=client.post('/api/clips/clip1/render',json={'layout':'stack','resolution':1280,'quality':'small'})
    job=wait(client,response.json()['id']); assert job['status']=='completed',job
    assert job['info']['layout_used']=='fit_blur' and job['info']['layout_fallback_reason']


@pytest.mark.parametrize('layout', ['follow','fit_blur','stack','auto'])
def test_shorts_hd_filter_integration(tmp_path, layout):
    import cv2
    from app.services import reframe, shorts
    ffmpeg=os.environ.get('CLIP_FFMPEG') or shutil.which('ffmpeg')
    ffprobe=os.environ.get('CLIP_FFPROBE') or shutil.which('ffprobe')
    if not ffmpeg or not ffprobe: pytest.skip('FFmpeg and FFprobe required')
    source=tmp_path/'source.mp4'; out=tmp_path/'out.mp4'
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=size=1280x720:rate=5','-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','1','-c:v','libx264','-preset','ultrafast','-c:a','aac','-y',str(source)],check=True)
    chosen=shorts.choose_layout(layout, shorts.estimate_upscale(1280,720,reframe.ReframeOptions()), two_tracks=layout=='stack')[0]
    cfg={'layout':chosen,'zoom':1,'horizontal':0,'vertical':0,'sharpen':.35,'fit_vertical_bias':-.05}
    samples=[reframe.Sample(i/5,faces=[(150,150,100,100,.9,0,0),(1000,150,100,100,.9,1,0)]) for i in range(5)]
    panels=reframe.panel_paths(samples,reframe.prominent_tracks(samples,1),1280,720,5,reframe.ReframeOptions(),5) if chosen=='stack' else None
    graph=rendering.filter_graph({'width':1280,'height':720},cfg,[{'t':0,'x':.5,'y':.5}],1080,1920,panels)
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-i',str(source),'-filter_complex',graph,'-map','[v]','-map','0:a:0','-t','1','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac','-y',str(out)],check=True)
    meta=media.probe_video(out)
    assert (meta['width'],meta['height'])==(1080,1920) and meta['has_audio']
    if chosen=='fit_blur':
        a=cv2.VideoCapture(str(source)); b=cv2.VideoCapture(str(out)); ok1,src=a.read(); ok2,dst=b.read(); a.release(); b.release()
        assert ok1 and ok2
        expected=cv2.resize(src,(1080,606),interpolation=cv2.INTER_AREA)
        # Downsampled foreground remains visibly faithful; background is substantially softened.
        foreground=dst[561:1167]  # (1920-606)/2 - 5% of canvas
        assert foreground.shape == expected.shape
        assert float(abs(foreground.astype('float32')-expected.astype('float32')).mean()) < 20
        gray=cv2.cvtColor(dst,cv2.COLOR_BGR2GRAY)
        top=cv2.Laplacian(gray[50:300],cv2.CV_64F).var()
        center=cv2.Laplacian(gray[700:950],cv2.CV_64F).var()
        assert top < center * .2


def test_preflight_is_cached_at_ingestion_and_lazy_for_old_video(app_with_video, monkeypatch):
    from app.services import preflight
    client, _, vid, _ = app_with_video
    with database.connect() as db:
        saved=db.execute('SELECT preflight_json FROM videos WHERE id=?',(vid,)).fetchone()[0]
    assert saved
    monkeypatch.setattr(preflight,'run',lambda *args: (_ for _ in ()).throw(AssertionError('cache missed')))
    cached=client.get(f'/api/videos/{vid}/preflight')
    assert cached.status_code==200 and cached.json()['stats']['sampled_frames']>0
    assert client.get('/api/videos/missing/preflight').status_code==404
    with database.connect() as db: db.execute('UPDATE videos SET preflight_json=NULL WHERE id=?',(vid,))
    result=client.get(f'/api/videos/{vid}/preflight').json()
    assert result['warnings'][0]['code']=='preflight_failed'
    assert client.get(f'/api/videos/{vid}/preflight').json()==result


def test_ingestion_preflight_failure_is_nonfatal(app_with_video, monkeypatch):
    from app.services import preflight
    client, project, _, tmp = app_with_video
    monkeypatch.setattr(preflight,'run',lambda *args: (_ for _ in ()).throw(ValueError('bad media')))
    data=(tmp/'source.mp4').read_bytes()
    upload=client.post('/api/videos/upload',json={'project_id':project,'filename':'another.mp4','size':len(data)}).json()
    assert client.put(f"/api/jobs/{upload['id']}/upload",content=data).status_code==202
    end=time.monotonic()+10
    while time.monotonic()<end:
        job=client.get(f"/api/jobs/{upload['id']}").json()
        if job['status'] in ('completed','failed'): break
        time.sleep(.05)
    assert job['status']=='completed',job
    result=client.get(f"/api/videos/{job['video_id']}/preflight").json()
    assert result['warnings'][0]['code']=='preflight_failed'


def test_shorts_fps_and_output_args():
    from app.services import shorts
    assert shorts.output_fps('60/1')==60
    assert shorts.output_fps('30000/1001')==25
    assert shorts.output_fps('120/1')==60
    assert shorts.output_fps('12/1')==12
    assert shorts.output_fps('24/1')==24
    with pytest.raises(ValueError): shorts.output_fps('0/0')
    assert shorts.video_encoder_args()[:2]==['-c:v','libx264']
    args=shorts.output_args(30)
    assert args[args.index('-pix_fmt')+1]=='yuv420p' and args[args.index('-ar')+1]=='48000'
    assert args[args.index('-color_primaries')+1]=='bt709'


def test_shorts_preset_output_and_loudness(app_with_video, monkeypatch):
    import json
    client, _, vid, tmp=app_with_video
    # EBU loudness gating requires enough audio; test a six-second tone.
    from app.services import ingestion
    path=ingestion.get_video(vid)['path']
    ffmpeg=os.environ.get('CLIP_FFMPEG') or shutil.which('ffmpeg')
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=size=320x180:rate=10','-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','6','-c:v','libx264','-preset','ultrafast','-c:a','aac','-y',path],check=True)
    with database.connect() as db:
        db.execute('UPDATE videos SET duration=6 WHERE id=?',(vid,))
        db.execute('UPDATE candidate_clips SET end=6 WHERE id=?',('clip1',))
    response=client.post('/api/clips/clip1/render',json={'preset':'shorts','layout':'fit_blur'})
    assert response.status_code==202,response.text
    result=wait(client,response.json()['id']); assert result['status']=='completed',result
    assert result['info']['preset']=='shorts' and result['info']['fps']==10
    assert result['info']['loudness']['before'] is not None
    assert result['info']['loudness']['after']==pytest.approx(-14,abs=1.5)
    output=tmp/'preset.mp4'; output.write_bytes(client.get(f"/api/renders/{result['id']}/download").content)
    ffprobe=os.environ.get('CLIP_FFPROBE') or shutil.which('ffprobe')
    data=json.loads(subprocess.check_output([ffprobe,'-v','error','-show_streams','-of','json',str(output)]))
    v=next(s for s in data['streams'] if s['codec_type']=='video')
    a=next(s for s in data['streams'] if s['codec_type']=='audio')
    assert (v['width'],v['height'],v['codec_name'],v['pix_fmt'])==(1080,1920,'h264','yuv420p')
    assert (a['codec_name'],a['sample_rate'],a['channels'])==('aac','48000',2)
    assert (v['color_space'],v['color_transfer'],v['color_primaries'])==('bt709','bt709','bt709')


def test_shorts_duration_limit_is_transactional(app_with_video):
    client, _, vid, _=app_with_video
    with database.connect() as db:
        db.execute('UPDATE videos SET duration=300 WHERE id=?',(vid,))
        db.execute('UPDATE candidate_clips SET end=181 WHERE id=?',('clip1',))
    r=client.post('/api/clips/clip1/render',json={'preset':'shorts'})
    assert r.status_code==409 and '180' in r.json()['detail']
    with database.connect() as db: assert not db.execute('SELECT 1 FROM renders').fetchone()


def test_shorts_measure_failure_skips_normalization(app_with_video, monkeypatch):
    client, _, _, _=app_with_video
    monkeypatch.setattr(rendering,'measure_loudness',lambda *args: None)
    response=client.post('/api/clips/clip1/render',json={'preset':'shorts','layout':'fit_blur'})
    done=wait(client,response.json()['id']); assert done['status']=='completed',done
    assert done['info']['loudness']=={'before':None,'after':None}
    assert 'not normalized' in done['info']['warnings'][0]


def test_shorts_tighten_synchronizes_synthetic_audio_and_video(app_with_video):
    import json
    from app.services import ingestion
    client, _, vid, tmp=app_with_video
    path=ingestion.get_video(vid)['path']
    ffmpeg=os.environ.get('CLIP_FFMPEG') or shutil.which('ffmpeg')
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=size=320x180:rate=25','-f','lavfi','-i',r"aevalsrc=if(between(t\,3\,5)\,0\,0.1*sin(2*PI*440*t)):s=48000",'-t','8','-c:v','libx264','-preset','ultrafast','-c:a','aac','-y',path],check=True)
    words=[{'start':.1,'end':2.9,'word':'one'},{'start':5.,'end':7.9,'word':'two.'}]
    with database.connect() as db:
        db.execute('UPDATE videos SET duration=8,frame_rate=? WHERE id=?',('25/1',vid))
        db.execute('UPDATE candidate_clips SET end=8 WHERE id=?',('clip1',))
        db.execute('INSERT INTO transcripts(video_id,model,language,duration) VALUES(?,?,?,?)',(vid,'base','en',8))
        db.execute('INSERT INTO transcript_segments(video_id,position,start,end,text,words_json) VALUES(?,?,?,?,?,?)',(vid,0,.1,7.9,'one two.',json.dumps(words)))
    response=client.post('/api/clips/clip1/render',json={'preset':'shorts','layout':'fit_blur','tighten':True})
    assert response.status_code==202,response.text
    result=wait(client,response.json()['id']); assert result['status']=='completed',result
    assert result['info']['removed_seconds']==pytest.approx(1.85,abs=.05)
    out=tmp/'tight.mp4'; out.write_bytes(client.get(f"/api/renders/{result['id']}/download").content)
    ffprobe=os.environ.get('CLIP_FFPROBE') or shutil.which('ffprobe')
    data=json.loads(subprocess.check_output([ffprobe,'-v','error','-show_streams','-of','json',str(out)]))
    v=next(s for s in data['streams'] if s['codec_type']=='video'); a=next(s for s in data['streams'] if s['codec_type']=='audio')
    assert abs(float(v['duration'])-float(a['duration']))<.08
    assert float(v['duration'])==pytest.approx(6.15,abs=.1)


@pytest.mark.parametrize('layout',['fit_blur','letterbox'])
def test_tightened_burned_ass_srt_vtt_caption_sync(app_with_video, monkeypatch, layout):
    """A real FFmpeg render and all subtitle formats share the edited timeline."""
    import json
    import re
    from app.services import ingestion
    client, _, vid, tmp = app_with_video
    source=ingestion.get_video(vid)['path']
    ffmpeg=os.environ.get('CLIP_FFMPEG') or shutil.which('ffmpeg')
    filters=subprocess.check_output([ffmpeg,'-hide_banner','-filters'],text=True)
    if not any(' ass ' in line for line in filters.splitlines()): pytest.skip('FFmpeg libass filter unavailable')
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=size=320x180:rate=25','-f','lavfi','-i',r'aevalsrc=if(between(t\,1.2\,3)\,0\,0.1*sin(2*PI*440*t)):s=48000','-t','8','-c:v','libx264','-preset','ultrafast','-c:a','aac','-y',source],check=True)
    words=[{'start':.2,'end':.6,'word':'Alpha.'},{'start':.8,'end':1.2,'word':'Beta.'},{'start':3.,'end':3.5,'word':'Gamma.'},{'start':3.7,'end':4.1,'word':'Delta.'}]
    with database.connect() as db:
        db.execute('UPDATE videos SET duration=8,frame_rate=? WHERE id=?',('25/1',vid))
        db.execute('UPDATE candidate_clips SET end=8 WHERE id=?',('clip1',))
        db.execute('INSERT INTO transcripts(video_id,model,language,duration) VALUES(?,?,?,?)',(vid,'base','en',8))
        db.execute('INSERT INTO transcript_segments(video_id,position,start,end,text,words_json) VALUES(?,?,?,?,?,?)',(vid,0,.2,4.1,'Alpha. Beta. Gamma. Delta.',json.dumps(words)))
    captured=[]; original=rendering.run_ffmpeg
    def observe(args,rid,duration,cwd=None):
        if cwd: captured.append((Path(cwd)/'captions.ass').read_text(encoding='utf-8'))
        return original(args,rid,duration,cwd=cwd)
    monkeypatch.setattr(rendering,'run_ffmpeg',observe)
    response=client.post('/api/clips/clip1/render',json={'preset':'shorts','layout':layout,'tighten':True,'captions':True,'caption_style':'highlight'})
    assert response.status_code==202,response.text
    job=wait(client,response.json()['id']); assert job['status']=='completed',job
    assert job['info']['removed_seconds']==pytest.approx(1.55,abs=.05)
    assert len(captured)==1
    def seconds(value):
        h,m,s=value.replace(',','.').split(':'); return int(h)*3600+int(m)*60+float(s)
    expected={'Alpha.':(.2,.6),'Beta.':(.8,1.2),'Gamma.':(1.45,1.95),'Delta.':(2.15,2.55)}
    found={}
    for line in captured[0].splitlines():
        if line.startswith('Dialogue:'):
            fields=line.split(',',9); match=re.search(r'\{\\c&H00FFFF&[^}]*\}([^{}]+)\{\\r\}',fields[9])
            if match: found[match.group(1)]=(seconds(fields[1]),seconds(fields[2]))
    assert set(found)==set(expected)
    for text,(start,end) in expected.items():
        assert found[text][0]==pytest.approx(start,abs=.08)
        assert found[text][1]==pytest.approx(end,abs=.08)
    for suffix in ('srt','vtt'):
        result=client.get(f"/api/clips/clip1/captions.{suffix}",params={'render_id':job['id']})
        assert result.status_code==200,result.text
        if suffix=='vtt': assert result.text.startswith('WEBVTT\n')
        cues=re.findall(r'(\d\d:\d\d:\d\d[.,]\d{3}) --> (\d\d:\d\d:\d\d[.,]\d{3})\s+([^\n]+)',result.text)
        assert len(cues)==4
        for start,end,text in cues:
            assert seconds(start)==pytest.approx(expected[text][0],abs=.08)
            assert seconds(end)==pytest.approx(expected[text][1],abs=.08)


def test_burned_captions_preflight_requires_override_and_batch_is_atomic(app_with_video):
    import json
    client, project, vid, _=app_with_video
    with database.connect() as db:
        db.execute('UPDATE videos SET preflight_json=? WHERE id=?',(json.dumps({'warnings':[{'code':'possible_burned_in_captions'}]}),vid))
    rendering.stop_worker()
    try:
        assert client.post('/api/clips/clip1/render',json={'captions':True}).status_code==409
        assert client.post(f'/api/projects/{project}/render-batch',json={'captions':True}).status_code==409
        with database.connect() as db: assert db.execute('SELECT COUNT(*) FROM renders').fetchone()[0]==0
        assert client.post('/api/clips/clip1/render',json={'captions':False}).status_code==202
        assert client.post('/api/clips/clip2/render',json={'captions':True,'force_captions':True}).status_code==202
    finally: rendering.start_worker()


def test_caption_render_snapshot_remains_after_transcript_changes(app_with_video):
    import json
    client, _, vid, _=app_with_video
    with database.connect() as db:
        db.execute('INSERT INTO transcripts(video_id,model,language,duration) VALUES(?,?,?,?)',(vid,'base','en',2))
        db.execute('INSERT INTO transcript_segments(video_id,position,start,end,text,words_json) VALUES(?,?,?,?,?,?)',(vid,0,.1,.8,'First.',json.dumps([{'start':.1,'end':.8,'word':'First.'}])))
    job=client.post('/api/clips/clip1/render',json={'layout':'center','resolution':1280}).json()
    done=wait(client,job['id']); assert done['status']=='completed',done
    with database.connect() as db: db.execute('UPDATE transcript_segments SET words_json=? WHERE video_id=?',(json.dumps([{'start':.2,'end':.7,'word':'Changed.'}]),vid))
    srt=client.get('/api/clips/clip1/captions.srt',params={'render_id':job['id']})
    assert srt.status_code==200 and 'First.' in srt.text and 'Changed.' not in srt.text
    assert client.get('/api/clips/clip2/captions.srt',params={'render_id':job['id']}).status_code==404


def test_loop_polish_filters_and_snap_render(app_with_video,monkeypatch):
    import json
    client, _, vid, _=app_with_video
    with database.connect() as db:
        db.execute('INSERT INTO transcripts(video_id,model,language,duration) VALUES(?,?,?,?)',(vid,'base','en',2))
        db.execute('INSERT INTO transcript_segments(video_id,position,start,end,text,words_json) VALUES(?,?,?,?,?,?)',(vid,0,.1,.7,'End.',json.dumps([{'start':.1,'end':.7,'word':'End.'}])))
    captured=[]; original=rendering.run_ffmpeg
    def observe(args,rid,duration,cwd=None):
        script=Path(args[args.index('-filter_complex_script')+1]).read_text()
        captured.append((args[:],script))
        return original(args,rid,duration,cwd=cwd)
    monkeypatch.setattr(rendering,'run_ffmpeg',observe)
    response=client.post('/api/clips/clip1/render',json={'preset':'shorts','layout':'fit_blur','loop_polish':True,'tighten':False})
    done=wait(client,response.json()['id']); assert done['status']=='completed',done
    assert done['info']['snapped_end']==pytest.approx(.85)
    script=captured[0][1]
    assert 'fade=t=in:st=0:d=0.1' in script and 'fade=t=out' in script
    assert 'afade=t=in:st=0:d=0.100' in script and 'afade=t=out' in script and 'd=0.150' in script


def test_metadata_api_transaction_and_render_sidecars(app_with_video,monkeypatch):
    import json
    from app.services import metadata
    client, project, vid, tmp=app_with_video
    monkeypatch.delenv('GROQ_API_KEY',raising=False); monkeypatch.delenv('GEMINI_API_KEY',raising=False)
    response=client.post('/api/clips/clip1/metadata',json={'tone':'curious','cloud':True})
    assert response.status_code==200 and response.json()['provider']=='basic (no AI)'
    saved=response.json()
    assert client.get('/api/clips/clip1/metadata').json()==saved
    monkeypatch.setenv('GROQ_API_KEY','fake')
    monkeypatch.setattr(metadata.httpx,'post',lambda *a,**k: __import__('httpx').Response(401))
    failed=client.post('/api/clips/clip1/metadata',json={'tone':'direct','cloud':True})
    assert failed.status_code==409 and client.get('/api/clips/clip1/metadata').json()==saved
    job=client.post('/api/clips/clip1/render',json={'layout':'center','resolution':1280}).json()
    done=wait(client,job['id']); assert done['status']=='completed',done
    root=tmp/'storage'/'rendered'/str(project)
    assert (root/(job['id']+'.txt')).exists()
    assert json.loads((root/(job['id']+'.metadata.json')).read_text())['titles']==saved['titles']
    assert client.delete('/api/renders/'+job['id']).status_code==204
    assert not (root/(job['id']+'.metadata.json')).exists()


def test_review_trim_revert_and_offsets(app_with_video):
    client, _, vid, _=app_with_video
    with database.connect() as db:
        db.execute('UPDATE videos SET duration=10 WHERE id=?',(vid,))
        db.execute('UPDATE candidate_clips SET start=1,end=6 WHERE id=?',('clip1',))
    assert client.patch('/api/clips/clip1',json={'start':2,'end':4}).status_code==422
    assert client.patch('/api/clips/clip1',json={'start':-1,'end':5}).status_code==422
    assert client.patch('/api/clips/clip1',json={'start':2,'end':20}).status_code==422
    first=client.patch('/api/clips/clip1',json={'start':2,'end':7})
    assert first.status_code==200 and (first.json()['original_start'],first.json()['original_end'])==(1,6)
    assert client.patch('/api/clips/clip1',json={'start':3,'end':8}).status_code==200
    reverted=client.post('/api/clips/clip1/revert')
    assert (reverted.json()['start'],reverted.json()['end'])==(1,6)
    assert rendering.apply_range(1,6,10,{'start_offset':-5,'end_offset':5})==(0,10)
    a,b=rendering.apply_range(1,6,10,{'start_offset':5,'end_offset':-5})
    assert b-a==3 and 0<=a<b<=10
    assert client.post('/api/clips/clip1/render',json={'x_offset':.36}).status_code==422


def test_preview_path_shape_cache_and_crop_offsets(app_with_video, monkeypatch):
    client, _, vid, _=app_with_video
    response=client.get('/api/clips/clip1/preview-path',params={'layout':'center','x_offset':.2})
    assert response.status_code==200,response.text
    data=response.json()
    assert (data['width'],data['height'],data['layout'])==(320,180,'center')
    assert len(data['path'])>=15 and set(data['path'][0])=={'t','x','y','w','h'}
    assert all(0<=p['x']<=320-p['w'] and 0<=p['y']<=180-p['h'] for p in data['path'])
    plain=client.get('/api/clips/clip1/preview-path',params={'layout':'center'}).json()
    assert data['path'][0]['x']>plain['path'][0]['x']
    assert client.get('/api/shorts/constants').json()['safe_zone']['bottom']==.2
    import app.services.rendering as renderer
    monkeypatch.setattr(renderer,'crop_with_offsets',lambda *a: (_ for _ in ()).throw(AssertionError('cache miss')))
    assert client.get('/api/clips/clip1/preview-path',params={'layout':'center','x_offset':.2}).json()==data


def test_trimmed_metadata_only_sends_words_within_trim(app_with_video,monkeypatch):
    import json
    from app.services import metadata
    client, _, vid, _=app_with_video
    with database.connect() as db:
        db.execute('UPDATE videos SET duration=10 WHERE id=?',(vid,))
        db.execute('UPDATE candidate_clips SET start=0,end=8 WHERE id=?',('clip1',))
        db.execute('INSERT INTO transcripts(video_id,model,language,duration) VALUES(?,?,?,?)',(vid,'base','en',10))
        words=[{'start':.2,'end':.8,'word':'outside'}, {'start':2.1,'end':2.6,'word':'inside.'}, {'start':7.2,'end':7.6,'word':'after'}]
        db.execute('INSERT INTO transcript_segments(video_id,position,start,end,text,words_json) VALUES(?,?,?,?,?,?)',(vid,0,.2,7.6,'outside inside. after',json.dumps(words)))
    assert client.patch('/api/clips/clip1',json={'start':2,'end':6}).status_code==200
    seen=[]
    def generate(text,title,tone,language,cloud):
        seen.append(text);return metadata.basic(text)
    monkeypatch.setattr(metadata,'generate',generate)
    result=client.post('/api/clips/clip1/metadata',json={'tone':'direct','cloud':True})
    assert result.status_code==200 and seen==['inside.']


def test_completed_render_reports_crop_offsets(app_with_video):
    client, _, _, _=app_with_video
    response=client.post('/api/clips/clip1/render',json={'layout':'center','resolution':1280,'x_offset':.2,'y_offset':-.1})
    assert response.status_code==202
    done=wait(client,response.json()['id']); assert done['status']=='completed',done
    assert done['info']['x_offset']==pytest.approx(.2) and done['info']['y_offset']==pytest.approx(-.1)


def test_offsets_are_stored_and_clamped_before_queue(app_with_video):
    client, _, vid, _=app_with_video
    with database.connect() as db:
        db.execute('UPDATE videos SET duration=10 WHERE id=?',(vid,))
        db.execute('UPDATE candidate_clips SET start=1,end=6 WHERE id=?',('clip1',))
    rendering.stop_worker()
    try:
        response=client.post('/api/clips/clip1/render',json={'layout':'center','start_offset':-5,'end_offset':5})
        assert response.status_code==202,response.text
        job=response.json(); assert job['start']==0 and job['end']==10
        assert job['settings']['start_offset']==-5 and job['settings']['end_offset']==5
        client.post('/api/renders/'+job['id']+'/cancel')
    finally: rendering.start_worker()


@pytest.mark.parametrize('size,color', [('1280x720','#000000'), ('1280x720','#101820'), ('1080x1920','#101820')])
def test_letterbox_hd_synthetic_bars_and_audio(tmp_path, size, color):
    import cv2
    import numpy as np
    from app.services import reframe
    ffmpeg=os.environ.get('CLIP_FFMPEG') or shutil.which('ffmpeg')
    if not ffmpeg: pytest.skip('FFmpeg required')
    width,height=map(int,size.split('x')); source=tmp_path/'in.mp4'; out=tmp_path/'out.mp4'
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i',f'testsrc2=size={size}:rate=3','-f','lavfi','-i','sine=frequency=440','-t','1','-c:v','libx264','-preset','ultrafast','-c:a','aac','-y',str(source)],check=True)
    rect=reframe.letterbox_rect(width,height)
    graph=rendering.filter_graph({'width':width,'height':height},{'layout':'letterbox','letterbox_bg':color},[],1080,1920)
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-i',str(source),'-filter_complex',graph,'-map','[v]','-map','0:a:0','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac','-y',str(out)],check=True)
    meta=media.probe_video(out); assert (meta['width'],meta['height'])==(1080,1920) and meta['has_audio']
    a=cv2.VideoCapture(str(source)); b=cv2.VideoCapture(str(out)); ok1,src=a.read(); ok2,dst=b.read(); a.release(); b.release(); assert ok1 and ok2
    expected=cv2.resize(src,(rect['w'],rect['h']),interpolation=cv2.INTER_AREA)
    actual=dst[rect['y']:rect['y']+rect['h'],rect['x']:rect['x']+rect['w']]
    assert np.abs(actual.astype(float)-expected.astype(float)).mean()<8
    if rect['h']<1920:
        expected_bgr=np.array([int(color[i:i+2],16) for i in (5,3,1)])
        for row in (dst[30],dst[-30]):
            assert row.var(axis=0).max()<1
            assert np.max(np.abs(row.mean(axis=0)-expected_bgr))<=2
        gray=cv2.cvtColor(dst,cv2.COLOR_BGR2GRAY)
        active=np.flatnonzero(np.mean(np.abs(gray.astype(float)-np.mean(gray[30])),axis=1)>3)
        assert abs(active[0]-rect['y'])<=2 and abs(active[-1]-(rect['y']+rect['h']-1))<=2
    else: assert rect=={'x':0,'y':0,'w':1080,'h':1920}


def test_letterbox_render_preview_captions_no_tracking(app_with_video,monkeypatch):
    from app.services import captions
    client, _, vid, _=app_with_video
    monkeypatch.setattr(rendering,'track',lambda *a,**k: (_ for _ in ()).throw(AssertionError('unexpected tracking')))
    monkeypatch.setattr(rendering,'shorts_track',lambda *a,**k: (_ for _ in ()).throw(AssertionError('unexpected tracking')))
    monkeypatch.setattr(rendering.analysis,'get_transcript',lambda _: {'segments':[{'words':[{'start':.1,'end':.6,'word':'Hello'},{'start':.6,'end':1,'word':'world'}]}]})
    captured={}; build=captions.build_ass
    def save_ass(*args,**kwargs):
        result=build(*args,**kwargs); captured['text']=result; captured['layout']=args[-1]; return result
    monkeypatch.setattr(captions,'build_ass',save_ass)
    cfg={'layout':'letterbox','letterbox_bg':'#101820','letterbox_vertical_bias':.1,'captions':True,'force_captions':True,'tighten':False,'quality':'small'}
    preview=client.get('/api/clips/clip1/preview-path',params=cfg)
    assert preview.status_code==200,preview.text
    p=preview.json(); assert p['layout']=='letterbox' and p['path'][0]['w']==320 and p['path'][0]['h']==180
    assert len(p['path'])==1 and p['letterbox_bg']=='#101820'
    response=client.post('/api/clips/clip1/render',json=cfg); assert response.status_code==202,response.text
    job=wait(client,response.json()['id']); assert job['status']=='completed',job
    assert job['info']['layout_used']=='letterbox' and job['info']['video_rect']==p['video_rect']
    assert job['info']['caption_position_used']=='on_video'
    assert captured['layout']['y']-2.4*captured['layout']['size']>=p['video_rect']['y']
    assert captured['layout']['y']<=p['video_rect']['y']+p['video_rect']['h']
    assert r'\pos(' in captured['text']
    assert client.get('/api/clips/clip1/preview-path',params={'layout':'letterbox','letterbox_bg':'red'}).status_code==422
