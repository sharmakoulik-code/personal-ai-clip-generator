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
