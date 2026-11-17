import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import ingestion, media


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('CLIP_DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('CLIP_STORAGE_PATH', str(tmp_path / 'storage'))
    monkeypatch.delenv('CLIP_DISABLE_WORKER', raising=False)
    with TestClient(app) as client:
        yield client, tmp_path


@pytest.fixture
def sample_video(tmp_path):
    ffmpeg = os.environ.get('CLIP_FFMPEG') or shutil.which('ffmpeg')
    ffprobe = os.environ.get('CLIP_FFPROBE') or shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('FFmpeg and FFprobe are required for real video tests')
    video = tmp_path / 'fixture.mp4'
    subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=10', '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', '-y', str(video)], check=True)
    return video


def wait_job(client, job_id, wanted=('completed', 'failed', 'cancelled')):
    for _ in range(200):
        job = client.get(f'/api/jobs/{job_id}').json()
        if job['status'] in wanted:
            return job
        time.sleep(.05)
    pytest.fail(f'Job did not finish: {job}')


@pytest.mark.parametrize('url', ['https://www.youtube.com/watch?v=abcdefghijk', 'https://youtu.be/abcdefghijk', 'https://www.youtube.com/shorts/abcdefghijk', 'https://www.youtube.com/live/abcdefghijk'])
def test_supported_youtube_urls(url):
    assert media.canonical_youtube_url(url) == ('https://www.youtube.com/watch?v=abcdefghijk', 'abcdefghijk')


@pytest.mark.parametrize('url', ['http://youtu.be/abcdefghijk', 'https://youtube.com.evil.test/watch?v=abcdefghijk', 'https://youtube.com/watch?v=bad', 'https://user@youtube.com/watch?v=abcdefghijk', 'https://youtu.be/abcdefghijk/extra', 'file:///etc/passwd'])
def test_reject_unsupported_urls(url):
    with pytest.raises(ValueError):
        media.canonical_youtube_url(url)


def test_real_upload_validation_and_restart(setup, sample_video, monkeypatch):
    client, tmp_path = setup
    project = client.post('/api/projects', json={'name': 'Upload test'}).json()
    file_bytes = sample_video.read_bytes()
    job = client.post('/api/videos/upload', json={'project_id': project['id'], 'filename': 'sample.mp4', 'size': len(file_bytes)}).json()
    response = client.put(f"/api/jobs/{job['id']}/upload", content=file_bytes)
    assert response.status_code == 202
    done = wait_job(client, job['id'])
    assert done['status'] == 'completed', done
    videos = client.get(f"/api/projects/{project['id']}/videos").json()
    assert len(videos) == 1
    video = videos[0]
    assert (video['width'], video['height'], video['has_audio']) == (160, 120, 1)
    assert video['duration'] > 0 and video['size_bytes'] == len(file_bytes)
    assert 'path' not in video and video['has_thumbnail']
    assert client.get(f"/api/videos/{video['id']}/thumbnail").status_code == 200
    stored = list((tmp_path / 'storage' / 'videos' / str(project['id'])).glob('*.mp4'))
    assert len(stored) == 1 and stored[0].read_bytes() == file_bytes
    # A new FastAPI lifespan uses the same on-disk SQLite database.
    ingestion.stop_worker()
    with TestClient(app) as restarted:
        assert restarted.get(f"/api/projects/{project['id']}/videos").json()[0]['id'] == video['id']
        assert restarted.get(f"/api/jobs/{job['id']}").json()['status'] == 'completed'
        assert restarted.delete(f"/api/videos/{video['id']}").status_code == 204
        assert not stored[0].exists()


def test_bad_uploads_and_cancel(setup):
    client, tmp = setup
    project_id = client.post('/api/projects', json={'name': 'Test'}).json()['id']
    assert client.post('/api/videos/upload', json={'project_id': project_id, 'filename': '../bad.mp4', 'size': 1}).status_code == 422
    assert client.post('/api/videos/upload', json={'project_id': project_id, 'filename': 'bad.exe', 'size': 1}).status_code == 422
    assert client.post('/api/videos/youtube/download', json={'project_id': project_id, 'url': 'https://notyoutube.com/a'}).status_code == 400
    job = client.post('/api/videos/upload', json={'project_id': project_id, 'filename': 'bad.mp4', 'size': 3}).json()
    assert client.put(f"/api/jobs/{job['id']}/upload", content=b'bad').status_code == 202
    assert wait_job(client, job['id'])['status'] == 'failed'
    assert not list((tmp / 'storage' / 'temp').rglob('*.mp4'))
    job = client.post('/api/videos/upload', json={'project_id': project_id, 'filename': 'cancel.mp4', 'size': 3}).json()
    assert client.post(f"/api/jobs/{job['id']}/cancel").json()['status'] == 'cancelled'
    assert client.put(f"/api/jobs/{job['id']}/upload", content=b'bad').status_code == 409


def test_mocked_youtube_metadata_and_download(setup, sample_video, monkeypatch):
    client, _ = setup
    captured = []
    class FakeYDL:
        def __init__(self, options): self.options = options
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, url, download):
            if download:
                self.options['progress_hooks'][0]({'status': 'downloading', 'downloaded_bytes': 50, 'total_bytes': 100, 'speed': 1000})
                captured.append(ingestion.list_jobs()[0]['progress'])
                dest = Path(self.options['outtmpl'].replace('%(ext)s', 'mp4'))
                shutil.copyfile(sample_video, dest)
            return {'id': 'abcdefghijk', 'title': 'Fixture video', 'channel': 'Fixture channel', 'duration': 1, 'formats': [{'format_id':'test', 'height':720, 'vcodec':'h264', 'ext':'mp4', 'filesize':100}]}
    monkeypatch.setattr(media, 'YoutubeDL', FakeYDL)
    monkeypatch.setattr(ingestion, 'YoutubeDL', FakeYDL)
    project_id = client.post('/api/projects', json={'name': 'Youtube'}).json()['id']
    url = 'https://youtu.be/abcdefghijk'
    info = client.post('/api/videos/youtube/metadata', json={'url': url})
    assert info.status_code == 200 and info.json()['title'] == 'Fixture video'
    job = client.post('/api/videos/youtube/download', json={'project_id': project_id, 'url': url, 'quality': '720', 'title': info.json()['title']}).json()
    assert wait_job(client, job['id'])['status'] == 'completed'
    assert captured == [50]
    assert len(client.get(f'/api/projects/{project_id}/videos').json()) == 1
    second = client.post('/api/videos/youtube/download', json={'project_id': project_id, 'url': url, 'quality': '720'}).json()
    assert wait_job(client, second['id'])['status'] == 'completed'
    assert len(client.get(f'/api/projects/{project_id}/videos').json()) == 1


def test_limits_wrong_container_and_missing_video(setup, sample_video, monkeypatch):
    client, tmp = setup
    project_id = client.post('/api/projects', json={'name': 'Limits'}).json()['id']
    monkeypatch.setenv('CLIP_MAX_UPLOAD_BYTES', '4')
    assert client.post('/api/videos/upload', json={'project_id': project_id, 'filename': 'big.mp4', 'size': 5}).status_code == 413
    monkeypatch.delenv('CLIP_MAX_UPLOAD_BYTES')
    # A real MP4 renamed to AVI is not an AVI container.
    data = sample_video.read_bytes()
    job = client.post('/api/videos/upload', json={'project_id': project_id, 'filename': 'wrong.avi', 'size': len(data)}).json()
    client.put(f"/api/jobs/{job['id']}/upload", content=data)
    done = wait_job(client, job['id'])
    assert done['status'] == 'failed'
    assert 'contents do not match' in done['error']
    assert client.get(f'/api/projects/{project_id}/videos').json() == []


def test_cancel_active_download_and_project_deletion(setup, monkeypatch):
    import threading
    client, _ = setup
    entered, resume = threading.Event(), threading.Event()
    class BlockingYDL:
        def __init__(self, options): self.options = options
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, url, download):
            entered.set()
            resume.wait(2)
            self.options['progress_hooks'][0]({'status': 'downloading', 'downloaded_bytes': 1, 'total_bytes': 10})
            return {}
    monkeypatch.setattr(ingestion, 'YoutubeDL', BlockingYDL)
    project_id = client.post('/api/projects', json={'name': 'Cancel'}).json()['id']
    job = client.post('/api/videos/youtube/download', json={'project_id': project_id, 'url': 'https://youtu.be/abcdefghijk'}).json()
    assert entered.wait(2)
    assert client.delete(f'/api/projects/{project_id}').status_code == 409
    client.post(f"/api/jobs/{job['id']}/cancel")
    resume.set()
    assert wait_job(client, job['id'])['status'] == 'cancelled'
    assert client.delete(f'/api/projects/{project_id}').status_code == 204


def test_missing_ffprobe_is_reported_before_upload(setup, monkeypatch):
    client, _ = setup
    project_id = client.post('/api/projects', json={'name': 'Missing tools'}).json()['id']
    monkeypatch.setenv('CLIP_FFPROBE', '/definitely/not/a/real/ffprobe')
    result = client.post('/api/videos/upload', json={'project_id': project_id, 'filename': 'video.mp4', 'size': 5})
    assert result.status_code == 503
    assert 'ffprobe' in result.json()['detail'].lower()
    assert client.get('/api/jobs').json() == []
