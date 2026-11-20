from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

import httpx
import pytest
from fastapi.testclient import TestClient
from app import database
from app.main import app
from app.services import media, youtube_upload as yt


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('CLIP_DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('CLIP_STORAGE_PATH', str(tmp_path / 'storage'))
    database.initialize(); yt.initialize()
    path = media.storage_path('rendered', 1, 'render1.mp4'); path.parent.mkdir(parents=True); path.write_bytes(b'mock mp4 data')
    row = {'id':'render1', 'project_id':1, 'status':'completed', 'path':str(path)}
    monkeypatch.setattr(yt.rendering, 'get', lambda rid: row if rid == 'render1' else None)
    monkeypatch.setattr(yt, '_token', {'access_token':'private-access-token'})
    return row, path


def test_only_finished_render_and_strict_generated_path(setup, monkeypatch):
    row, path = setup
    calls = []; monkeypatch.setattr(yt.httpx, 'post', lambda *a, **k: calls.append(a))
    args = ('title', 'description', '2099-01-01T00:00:00Z', False)
    with pytest.raises(yt.YouTubeError): yt.upload('missing', *args)
    row['status'] = 'pending'
    with pytest.raises(yt.YouTubeError): yt.upload('render1', *args)
    row['status'] = 'completed'; row['path'] = str(path.parent / 'another.mp4')
    with pytest.raises(yt.YouTubeError): yt.upload('render1', *args)
    assert not calls


def test_upload_schedule_metadata_and_duplicate_protection(setup, monkeypatch):
    seen = []
    def post(url, **kwargs):
        seen.append(('post', url, kwargs))
        return httpx.Response(200, headers={'location':'https://www.googleapis.com/upload/youtube/v3/videos?upload_id=example'})
    def put(url, **kwargs):
        seen.append(('put', url, kwargs)); assert b''.join(kwargs['content']) == b'mock mp4 data'
        return httpx.Response(200, json={'id':'abcDEF12345', 'status':{'privacyStatus':'private','publishAt':'2099-01-01T00:00:00Z'}})
    monkeypatch.setattr(yt.httpx, 'post', post); monkeypatch.setattr(yt.httpx, 'put', put)
    result = yt.upload('render1', 'My title', 'My description', '2099-01-01T00:00:00Z', False)
    assert result['status'] == 'uploaded'
    assert seen[0][2]['json'] == {'snippet':{'title':'My title','description':'My description'},'status':{'privacyStatus':'private','publishAt':'2099-01-01T00:00:00Z','selfDeclaredMadeForKids':False}}
    assert all('transcript' not in str(c[2].get('json','')) for c in seen)
    with pytest.raises(yt.YouTubeError, match='already uploaded'): yt.upload('render1','x','','2099-01-01T00:00:00Z',False)
    assert len(seen) == 2


def test_host_redirect_and_uncertain_outcome_are_safe(setup, monkeypatch):
    monkeypatch.setattr(yt.httpx, 'post', lambda *a, **k: httpx.Response(200, headers={'location':'https://evil.example/upload/youtube/v3/videos'}))
    monkeypatch.setattr(yt.httpx, 'put', lambda *a, **k: pytest.fail('must not send file to untrusted host'))
    with pytest.raises(yt.YouTubeError, match='invalid upload destination'): yt.upload('render1','t','','2099-01-01T00:00:00Z',False)
    monkeypatch.setattr(yt.httpx, 'post', lambda *a, **k: httpx.Response(200, headers={'location':'https://www.googleapis.com/upload/youtube/v3/videos?upload_id=x'}))
    def failure(*a, **k): raise httpx.ReadTimeout('private details')
    monkeypatch.setattr(yt.httpx, 'put', failure)
    with pytest.raises(yt.YouTubeError, match='result is unknown') as err: yt.upload('render1','t','','2099-01-01T00:00:00Z',False)
    assert 'private details' not in str(err.value)
    with pytest.raises(yt.YouTubeError, match='already uploaded'): yt.upload('render1','t','','2099-01-01T00:00:00Z',False)
    yt.initialize()  # A restart preserves ambiguous outcomes.
    with database.connect() as db: assert db.execute('SELECT status FROM youtube_uploads').fetchone()['status'] == 'unknown'


def test_unconfirmed_schedule_not_claimed(setup, monkeypatch):
    monkeypatch.setattr(yt.httpx, 'post', lambda *a, **k: httpx.Response(200, headers={'location':'https://www.googleapis.com/upload/youtube/v3/videos?upload_id=x'}))
    monkeypatch.setattr(yt.httpx, 'put', lambda *a, **k: httpx.Response(200, json={'id':'abcDEF12345','status':{'privacyStatus':'private'}}))
    assert yt.upload('render1','t','','2099-01-01T00:00:00Z',False)['status'] == 'schedule_unconfirmed'


def test_route_requires_local_origin_and_valid_future_time(setup, monkeypatch):
    client = TestClient(app)
    headers = {'host':'127.0.0.1:5173','origin':'http://127.0.0.1:5173','x-clip-studio':'youtube-upload'}
    now = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    body = {'render_id':'render1','title':'title','description':'description','publish_at':now,'made_for_kids':False}
    assert client.post('/api/youtube/upload',json=body).status_code == 403
    assert client.post('/api/youtube/upload',json=body,headers=headers | {'origin':'https://evil.example'}).status_code == 403
    assert client.get('/api/youtube/status',headers={'host':'evil.example:8000','x-clip-studio':'youtube-upload'}).status_code == 403
    assert client.get('/api/youtube/status',headers={'host':'127.0.0.1:5173','x-clip-studio':'youtube-upload'}).status_code == 200
    assert client.post('/api/youtube/upload',json=body | {'publish_at':'2020-01-01T00:00:00Z'},headers=headers).status_code == 422
    assert client.post('/api/youtube/upload',json=body | {'publish_at':'2099-01-01T00:00:00'},headers=headers).status_code == 422
    assert client.post('/api/youtube/upload',json=body | {'title':'   '},headers=headers).status_code == 422
    assert client.get('/api/youtube/status',headers=headers).json()['connected'] is True


def test_oauth_pkce_local_callback_and_no_token_in_url(monkeypatch):
    monkeypatch.setenv('YOUTUBE_CLIENT_ID','test-client'); monkeypatch.setenv('YOUTUBE_CLIENT_SECRET','test-secret')
    monkeypatch.setattr(yt, '_pending', False); monkeypatch.setattr(yt, '_token', None)
    calls=[]
    def exchange(url, **kwargs):
        calls.append(kwargs['data'])
        return httpx.Response(200,json={'access_token':'secret-access-token'})
    monkeypatch.setattr(yt.httpx, 'post', exchange)
    url = yt.connect(); parsed = parse_qs(urlsplit(url).query)
    assert parsed['code_challenge_method'] == ['S256'] and 'test-secret' not in url
    with urlopen(parsed['redirect_uri'][0] + '?state=' + parsed['state'][0] + '&code=test-code', timeout=5) as result:
        assert b'secret-access-token' not in result.read()
    assert calls[0]['code'] == 'test-code' and calls[0]['code_verifier'] and yt.status()['connected']
    yt.disconnect(); assert not yt.status()['connected']


def test_rejects_render_file_symlink_to_other_media(setup, monkeypatch, tmp_path):
    row, path = setup
    source = tmp_path / 'storage' / 'videos' / '1' / 'source.mp4'
    source.parent.mkdir(parents=True); source.write_bytes(b'private source')
    path.unlink()
    try: path.symlink_to(source)
    except OSError: pytest.skip('File symlinks unavailable on this platform')
    monkeypatch.setattr(yt.httpx, 'post', lambda *a, **kw: pytest.fail('must not upload source media'))
    with pytest.raises(yt.YouTubeError, match='not available'):
        yt.upload('render1', 'title', '', '2099-01-01T00:00:00Z', False)
