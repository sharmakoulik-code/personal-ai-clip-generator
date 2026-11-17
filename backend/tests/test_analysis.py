import os
import shutil
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import database
from app.services import analysis, clip_selection, media


def wait_job(client, job_id, limit=8):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        job = client.get(f'/api/analysis/jobs/{job_id}').json()
        if job['status'] in ('completed', 'failed', 'cancelled'):
            return job
        time.sleep(.05)
    pytest.fail(f'Analysis did not finish: {job}')


def make_video(project_id, path, *, audio=True, duration=50):
    with database.connect() as db:
        db.execute('''INSERT INTO videos (id,project_id,source_type,title,path,duration,width,height,video_codec,has_audio,size_bytes)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?)''', ('vid', project_id, 'upload', 'Sample', str(path), duration, 160, 120, 'h264', int(audio), 1))


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv('CLIP_DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('CLIP_STORAGE_PATH', str(tmp_path / 'storage'))
    monkeypatch.delenv('CLIP_DISABLE_WORKER', raising=False)
    with TestClient(app) as client:
        project_id = client.post('/api/projects', json={'name': 'Analysis'}).json()['id']
        video_path = media.storage_path('videos', project_id, 'vid.mp4')
        video_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.write_bytes(b'source')
        make_video(project_id, video_path)
        yield client, project_id


def fake_segments():
    return [
        {'start': 0, 'end': 9, 'text': 'Why is this important?', 'words': []},
        {'start': 9.2, 'end': 18, 'text': 'Because it solves a surprising problem.', 'words': []},
        {'start': 18.2, 'end': 27, 'text': 'The solution is simple and useful.', 'words': []},
        {'start': 27.2, 'end': 36, 'text': 'Remember this amazing lesson!', 'words': []},
    ]


def test_candidate_boundaries_scoring_and_overlap():
    segments = fake_segments()
    clips = clip_selection.generate_candidates(segments, 15, 30)
    assert clips
    for clip in clips:
        assert 15 <= clip['end'] - clip['start'] <= 30
        assert 0 <= clip['score'] <= 100
        assert set(clip['scores']) == {'hook_strength', 'information', 'emotion', 'context_completeness', 'standalone', 'ending'}
        assert clip['hook'] in ' '.join(s['text'] for s in segments)
        assert 'Heuristic' in clip['reason']
    for index, clip in enumerate(clips):
        for other in clips[index + 1:]:
            overlap = max(0, min(clip['end'], other['end']) - max(clip['start'], other['start']))
            assert overlap / min(clip['end'] - clip['start'], other['end'] - other['start']) <= .5
    assert clip_selection.generate_candidates(segments[:1], 15, 30) == []


def test_word_level_boundaries_preserve_text():
    sentence = clip_selection.sentences([{'start': 1, 'end': 3, 'text': 'Hello world.', 'words': [{'start': 1, 'end': 1.5, 'word': ' Hello'}, {'start': 1.7, 'end': 3, 'word': ' world.'}]}])
    assert sentence == [{'start': 1, 'end': 3, 'text': 'Hello world.'}]
    chinese = clip_selection.sentences([{'start': 0, 'end': 2, 'text': '你好。', 'words': [{'start': 0, 'end': 1, 'word': '你'}, {'start': 1, 'end': 2, 'word': '好。'}]}])
    assert chinese[0]['text'] == '你好。'


def test_analysis_save_selection_reuse_and_restart(configured, monkeypatch):
    client, project_id = configured
    calls = []
    def fake_transcribe(video, job):
        calls.append(job['id'])
        return 'en', fake_segments()
    monkeypatch.setattr(analysis, 'transcribe', fake_transcribe)
    result = client.post('/api/videos/vid/analyze', json={'model': 'base', 'duration_range': '15-30'})
    assert result.status_code == 202
    assert wait_job(client, result.json()['id'])['status'] == 'completed'
    transcript = client.get('/api/videos/vid/transcription').json()
    assert transcript['language'] == 'en' and transcript['segments'][0]['start'] == 0
    clips = client.get('/api/videos/vid/clips').json()
    assert clips and all(not c['selected'] for c in clips)
    clip_id = clips[0]['id']
    assert client.patch(f'/api/clips/{clip_id}', json={'selected': True}).json()['selected'] is True
    assert client.get(f'/api/clips/{clip_id}').json()['selected'] is True
    all_clips = client.patch('/api/videos/vid/clips/selection', json={'selected': True}).json()
    assert all(c['selected'] for c in all_clips)
    assert client.delete('/api/videos/vid').status_code == 204
    assert client.get('/api/clips/' + clip_id).status_code == 404
    # Recreate and verify persisted results survive a fresh app lifespan.
    video_path = media.storage_path('videos', project_id, 'vid.mp4'); video_path.write_bytes(b'source')
    make_video(project_id, video_path)
    job = client.post('/api/videos/vid/analyze', json={'model': 'base', 'duration_range': '15-30'}).json()
    assert wait_job(client, job['id'])['status'] == 'completed'
    all_clips = client.patch('/api/videos/vid/clips/selection', json={'selected': True}).json()
    analysis.stop_worker()
    with TestClient(app) as restarted:
        assert restarted.get('/api/videos/vid/clips').json() == all_clips
        assert restarted.get('/api/videos/vid/transcription').json()['segments'] == fake_segments()
        second = restarted.post('/api/videos/vid/analyze', json={'model': 'base', 'duration_range': '15-30'}).json()
        assert wait_job(restarted, second['id'])['status'] == 'completed'
        assert len(calls) == 2  # two originals, then reused transcript


def test_no_audio_and_invalid_options(configured):
    client, _ = configured
    with database.connect() as db: db.execute('UPDATE videos SET has_audio=0 WHERE id=?', ('vid',))
    assert client.post('/api/videos/vid/analyze', json={}).status_code == 422
    assert client.post('/api/videos/vid/analyze', json={'model': 'untrusted'}).status_code == 422
    assert client.get('/api/videos/no-such-video/transcription').status_code == 404


def test_active_job_cancellation_and_delete_protection(configured, monkeypatch):
    client, project_id = configured
    entered, release = threading.Event(), threading.Event()
    def blocked(video, job):
        entered.set(); release.wait(3); analysis.check_cancel(job['id'])
        return 'en', fake_segments()
    monkeypatch.setattr(analysis, 'transcribe', blocked)
    job = client.post('/api/videos/vid/analyze', json={'duration_range': '15-30'}).json()
    assert entered.wait(2)
    assert client.post('/api/videos/vid/analyze', json={}).status_code == 409
    assert client.delete('/api/videos/vid').status_code == 409
    assert client.delete(f'/api/projects/{project_id}').status_code == 409
    assert client.post(f"/api/analysis/jobs/{job['id']}/cancel").status_code == 200
    release.set()
    assert wait_job(client, job['id'])['status'] == 'cancelled'
    assert client.get('/api/videos/vid/clips').json() == []


def test_failed_analysis_preserves_existing_data(configured, monkeypatch):
    client, _ = configured
    monkeypatch.setattr(analysis, 'transcribe', lambda video, job: ('en', fake_segments()))
    job = client.post('/api/videos/vid/analyze', json={'duration_range': '15-30'}).json()
    assert wait_job(client, job['id'])['status'] == 'completed'
    original = client.get('/api/videos/vid/clips').json()
    def fail(video, job): raise ValueError('No audio to analyze')
    monkeypatch.setattr(analysis, 'transcribe', fail)
    second = client.post('/api/videos/vid/analyze', json={'duration_range': '15-30', 'force_transcribe': True}).json()
    assert wait_job(client, second['id'])['status'] == 'failed'
    assert client.get('/api/videos/vid/clips').json() == original


def test_real_whisper_jfk_video(tmp_path, monkeypatch):
    """Optional real inference integration; requires a local model and real speech video."""
    model = os.environ.get('CLIP_TEST_MODEL_DIR'); source = os.environ.get('CLIP_TEST_SPEECH_VIDEO')
    if not model or not source: pytest.skip('Set CLIP_TEST_MODEL_DIR and CLIP_TEST_SPEECH_VIDEO to run real Whisper inference')
    monkeypatch.setenv('CLIP_DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('CLIP_STORAGE_PATH', str(tmp_path / 'storage'))
    monkeypatch.delenv('CLIP_DISABLE_WORKER', raising=False)
    shutil.copytree(model, tmp_path / 'storage' / 'models' / 'whisper-base')
    data = Path(source).read_bytes()
    with TestClient(app) as client:
        project_id = client.post('/api/projects', json={'name': 'Real speech'}).json()['id']
        upload = client.post('/api/videos/upload', json={'project_id': project_id, 'filename': 'speech.mp4', 'size': len(data)}).json()
        assert client.put(f"/api/jobs/{upload['id']}/upload", content=data).status_code == 202
        for _ in range(200):
            imported = client.get(f"/api/jobs/{upload['id']}").json()
            if imported['status'] in ('completed', 'failed'): break
            time.sleep(.05)
        assert imported['status'] == 'completed', imported
        video_id = imported['video_id']
        job = client.post(f'/api/videos/{video_id}/analyze', json={'model': 'base', 'duration_range': '15-30'}).json()
        done = wait_job(client, job['id'], 180)
        assert done['status'] == 'completed', done
        transcript = client.get(f'/api/videos/{video_id}/transcription').json()
        text = ' '.join(s['text'] for s in transcript['segments']).lower()
        print('REAL WHISPER TRANSCRIPT:', text)
        assert 'ask not' in text and 'country' in text
        assert transcript['segments'] and all(0 <= s['start'] < s['end'] <= transcript['duration'] + .01 for s in transcript['segments'])
        assert any(s['words'] for s in transcript['segments'])
        words = [w for s in transcript['segments'] for w in s['words']]
        # In the public JFK fixture, the first 'ask' is spoken a few seconds in.
        first_ask = next(w for w in words if w['word'].strip().lower() == 'ask')
        assert 1 <= first_ask['start'] <= 5 and first_ask['end'] > first_ask['start']
        clips = client.get(f'/api/videos/{video_id}/clips').json()
        print('REAL CLIPS:', [(c['start'], c['end'], c['score']) for c in clips])
        assert clips and all(15 <= c['end'] - c['start'] <= 30 and c['end'] <= transcript['duration'] + .01 for c in clips)


def test_real_video_without_audio(tmp_path, monkeypatch):
    ffmpeg = os.environ.get('CLIP_FFMPEG') or shutil.which('ffmpeg')
    ffprobe = os.environ.get('CLIP_FFPROBE') or shutil.which('ffprobe')
    if not ffmpeg or not ffprobe: pytest.skip('FFmpeg and FFprobe required')
    monkeypatch.setenv('CLIP_DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('CLIP_STORAGE_PATH', str(tmp_path / 'storage'))
    source = tmp_path / 'silent.mp4'
    import subprocess
    subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=black:size=160x120:rate=5', '-t', '1', '-c:v', 'libx264', '-y', str(source)], check=True)
    data = source.read_bytes()
    with TestClient(app) as client:
        project = client.post('/api/projects', json={'name': 'No audio'}).json()
        job = client.post('/api/videos/upload', json={'project_id': project['id'], 'filename': 'silent.mp4', 'size': len(data)}).json()
        assert client.put(f"/api/jobs/{job['id']}/upload", content=data).status_code == 202
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = client.get(f"/api/jobs/{job['id']}").json()
            if result['status'] in ('completed', 'failed'): break
            time.sleep(.05)
        assert result['status'] == 'completed', result
        refusal = client.post(f"/api/videos/{result['video_id']}/analyze", json={})
        assert refusal.status_code == 422 and 'no audio' in refusal.json()['detail'].lower()


def test_long_audio_windows_do_not_duplicate_overlap(configured, monkeypatch):
    from types import SimpleNamespace
    client, project_id = configured
    with database.connect() as db:
        db.execute("UPDATE videos SET duration=65 WHERE id='vid'")
    windows = []
    def extract(source, target, start, length, job_id):
        windows.append((start, length))
        target.write_bytes(b'wave')
    class Model:
        def transcribe(self, path, **kwargs):
            # The same word straddles the 60-second boundary in both windows.
            if len(windows) == 1:
                words = [SimpleNamespace(start=5, end=6, word=' Early.'), SimpleNamespace(start=59.8, end=60.2, word=' Shared.')]
            else:
                words = [SimpleNamespace(start=.8, end=1.2, word=' Shared.'), SimpleNamespace(start=3, end=4, word=' Later.')]
            segment = SimpleNamespace(start=words[0].start, end=words[-1].end, words=words, text='')
            return iter([segment]), SimpleNamespace(language='en')
    monkeypatch.setattr(analysis, 'extract_chunk', extract)
    monkeypatch.setattr(analysis, 'whisper_model', lambda name: Model())
    job = client.post('/api/videos/vid/analyze', json={'duration_range': '15-30'}).json()
    done = wait_job(client, job['id'])
    assert done['status'] == 'completed', done
    transcript = client.get('/api/videos/vid/transcription').json()
    words = [w['word'].strip() for segment in transcript['segments'] for w in segment['words']]
    assert words == ['Early.', 'Shared.', 'Later.']
    assert windows == [(0.0, 61.0), (59.0, 6.0)]


def test_unpunctuated_speech_is_split_at_word_boundaries():
    words = [{'start': float(i), 'end': i + .8, 'word': f' word{i}'} for i in range(50)]
    segments = [{'start': 0, 'end': 49.8, 'text': ''.join(w['word'] for w in words), 'words': words}]
    units = clip_selection.sentences(segments)
    assert len(units) > 1
    assert all(u['end'] - u['start'] <= 13 for u in units)
    candidates = clip_selection.generate_candidates(segments, 15, 30)
    assert candidates and all(15 <= c['end'] - c['start'] <= 30 for c in candidates)
    assert all(c['transcript'].replace(' ', '') in segments[0]['text'].replace(' ', '') for c in candidates)


def test_groq_enhances_cached_transcript_without_changing_timestamps(configured, monkeypatch):
    import httpx
    from app.services import groq_enhancement
    client, _ = configured
    calls = []
    monkeypatch.setenv('GROQ_API_KEY', 'test-only-key')
    monkeypatch.setattr(analysis, 'transcribe', lambda video, job: ('en', fake_segments()))
    original_job = client.post('/api/videos/vid/analyze', json={'duration_range': '15-30'}).json()
    assert wait_job(client, original_job['id'])['status'] == 'completed'
    original = client.get('/api/videos/vid/clips').json()
    def fake_post(url, **kwargs):
        assert url == 'https://api.groq.com/openai/v1/chat/completions'
        assert kwargs['headers']['Authorization'] == 'Bearer test-only-key'
        assert kwargs['follow_redirects'] is False
        request = kwargs['json']
        assert request['model'] == 'openai/gpt-oss-20b'
        assert request['reasoning_format'] == 'hidden' and request['reasoning_effort'] == 'low'
        assert request['response_format'] == {'type': 'json_object'}
        assert request['max_completion_tokens'] >= 4096
        import json
        payload = json.loads(kwargs['json']['messages'][1]['content'])
        calls.append(payload)
        content = json.dumps({'clips': [{'id': c['id'], 'title': 'A useful lesson', 'reason': 'Explains a useful idea.'} for c in payload]})
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
    monkeypatch.setattr(groq_enhancement.httpx, 'post', fake_post)
    def must_not_transcribe(*args): raise AssertionError('should reuse transcript')
    monkeypatch.setattr(analysis, 'transcribe', must_not_transcribe)
    second = client.post('/api/videos/vid/analyze', json={'duration_range': '15-30', 'groq_titles': True}).json()
    assert wait_job(client, second['id'])['status'] == 'completed'
    enhanced = client.get('/api/videos/vid/clips').json()
    assert calls and all(c['title'] == 'A useful lesson' and c['reason'].startswith('Groq suggestion (review):') for c in enhanced)
    assert [(c['start'], c['end'], c['score'], c['hook']) for c in enhanced] == [(c['start'], c['end'], c['score'], c['hook']) for c in original]
    assert 'test-only-key' not in str(client.get('/api/videos/vid/analysis-jobs').json())


def test_groq_missing_key_and_failure_preserve_existing_results(configured, monkeypatch):
    import httpx
    from app.services import groq_enhancement
    client, _ = configured
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    assert client.post('/api/videos/vid/analyze', json={'groq_titles': True}).status_code == 409
    monkeypatch.setattr(analysis, 'transcribe', lambda video, job: ('en', fake_segments()))
    first = client.post('/api/videos/vid/analyze', json={'duration_range': '15-30'}).json()
    assert wait_job(client, first['id'])['status'] == 'completed'
    old = client.get('/api/videos/vid/clips').json()
    monkeypatch.setenv('GROQ_API_KEY', 'test-only-key')
    monkeypatch.setattr(groq_enhancement.httpx, 'post', lambda *args, **kwargs: httpx.Response(429, text='secret text not exposed'))
    second = client.post('/api/videos/vid/analyze', json={'duration_range': '15-30', 'groq_titles': True}).json()
    failed = wait_job(client, second['id'])
    assert failed['status'] == 'failed' and 'quota' in failed['error']
    assert 'secret' not in failed['error'] and 'test-only-key' not in str(failed)
    assert client.get('/api/videos/vid/clips').json() == old


def test_groq_rejects_fabricated_candidate_ids(monkeypatch):
    import httpx
    from app.services import groq_enhancement
    monkeypatch.setenv('GROQ_API_KEY', 'test-only-key')
    monkeypatch.setattr(groq_enhancement.httpx, 'post', lambda *a, **kw: httpx.Response(200, json={'choices': [{'message': {'content': '{"clips":[{"id":"wrong","title":"x","reason":"y"}]}'}}]}))
    with pytest.raises(ValueError, match='invalid response'):
        groq_enhancement.enhance([{'id': 'real', 'transcript': 'Actual words.'}])


def test_groq_column_migrates_existing_database(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE analysis_jobs (id TEXT PRIMARY KEY, video_id TEXT, status TEXT)')
        db.execute("INSERT INTO analysis_jobs (id, video_id, status) VALUES ('old', 'video', 'completed')")
    monkeypatch.setenv('CLIP_DB_PATH', str(path))
    database.initialize()
    database.initialize()  # migration is idempotent
    with database.connect() as db:
        row = db.execute("SELECT id, groq_titles FROM analysis_jobs WHERE id='old'").fetchone()
        assert row['groq_titles'] == 0


def test_groq_network_errors_do_not_expose_details(monkeypatch):
    import httpx
    from app.services import groq_enhancement
    monkeypatch.setenv('GROQ_API_KEY', 'test-only-key')
    def fail(*a, **kw): raise httpx.ConnectError('sensitive detail test-only-key')
    monkeypatch.setattr(groq_enhancement.httpx, 'post', fail)
    with pytest.raises(ValueError) as err:
        groq_enhancement.enhance([{'id': 'real', 'transcript': 'Actual words.'}])
    assert 'test-only-key' not in str(err.value) and 'sensitive' not in str(err.value)


def test_groq_model_unavailable_error_is_safe(monkeypatch):
    import httpx
    from app.services import groq_enhancement
    monkeypatch.setenv('GROQ_API_KEY', 'test-only-key')
    monkeypatch.setattr(groq_enhancement.httpx, 'post', lambda *a, **kw: httpx.Response(404, text='secret server response'))
    with pytest.raises(ValueError, match='model or endpoint unavailable') as err:
        groq_enhancement.enhance([{'id': 'real', 'transcript': 'Actual words.'}])
    assert 'secret' not in str(err.value) and 'test-only-key' not in str(err.value)


def test_groq_truncated_output_is_rejected(monkeypatch):
    import httpx
    from app.services import groq_enhancement
    monkeypatch.setenv('GROQ_API_KEY', 'test-only-key')
    body = {'choices': [{'finish_reason': 'length', 'message': {'content': '{"clips":[{"id":"real","title":"x","reason":"y"}]}'}}]}
    monkeypatch.setattr(groq_enhancement.httpx, 'post', lambda *a, **kw: httpx.Response(200, json=body))
    with pytest.raises(ValueError, match='invalid response'):
        groq_enhancement.enhance([{'id': 'real', 'transcript': 'Actual words.'}])
