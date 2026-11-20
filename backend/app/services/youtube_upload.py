"""Opt-in YouTube uploads. OAuth tokens stay in memory and only renders can be sent."""
import base64
import hashlib
import os
import secrets
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from .. import database
from . import media, rendering

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
UPLOAD_URL = 'https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status'
SCOPE = 'https://www.googleapis.com/auth/youtube.upload'
_lock = threading.RLock()
_token = None
_pending = False

class YouTubeError(Exception):
    pass


def initialize():
    with database.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS youtube_uploads (
          render_id TEXT PRIMARY KEY, status TEXT NOT NULL, video_id TEXT,
          title TEXT NOT NULL, publish_at TEXT, updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )''')
        db.execute("UPDATE youtube_uploads SET status='unknown' WHERE status='uploading'")


def config():
    return os.environ.get('YOUTUBE_CLIENT_ID', '').strip(), os.environ.get('YOUTUBE_CLIENT_SECRET', '').strip()


def status():
    with _lock:
        return {'configured': bool(config()[0]), 'connected': bool(_token), 'connecting': _pending}


def disconnect():
    global _token
    with _lock: _token = None


def connect():
    global _pending
    client_id, client_secret = config()
    if not client_id: raise YouTubeError('Set YOUTUBE_CLIENT_ID and restart the app first.')
    with _lock:
        if _pending: raise YouTubeError('A sign-in is already in progress. Wait a few minutes and try again.')
        _pending = True
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass  # Never log authorization codes.
        def do_GET(self):
            global _token
            query = parse_qs(urlsplit(self.path).query)
            ok = False
            if urlsplit(self.path).path == '/' and secrets.compare_digest(query.get('state', [''])[0], state) and len(query.get('code', [])) == 1:
                try:
                    data = {'code': query['code'][0], 'client_id': client_id, 'redirect_uri': redirect,
                            'grant_type': 'authorization_code', 'code_verifier': verifier}
                    if client_secret: data['client_secret'] = client_secret
                    response = httpx.post(TOKEN_URL, data=data, timeout=20, follow_redirects=False)
                    token = response.json() if response.status_code == 200 else {}
                    if isinstance(token.get('access_token'), str):
                        with _lock: _token = token
                        ok = True
                except (httpx.HTTPError, ValueError): pass
            text = ('YouTube connected. You may close this tab and return to the app.' if ok else 'YouTube sign-in failed or was cancelled. Return to the app and try again.')
            payload = text.encode()
            self.send_response(200 if ok else 400); self.send_header('Content-Type','text/plain; charset=utf-8')
            self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(payload))); self.end_headers(); self.wfile.write(payload)
    try:
        server = HTTPServer(('127.0.0.1', 0), Handler)
    except OSError:
        with _lock: _pending = False
        raise YouTubeError('Could not start the local sign-in listener.') from None
    redirect = f'http://127.0.0.1:{server.server_port}/'
    server.timeout = 300
    def wait():
        global _pending
        try: server.handle_request()
        finally:
            server.server_close()
            with _lock: _pending = False
    threading.Thread(target=wait, daemon=True).start()
    params = {'client_id': client_id, 'redirect_uri': redirect, 'response_type': 'code', 'scope': SCOPE,
              'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'}
    return AUTH_URL + '?' + urlencode(params)


def history(project_id):
    with database.connect() as db:
        rows = db.execute('''SELECT u.render_id,u.status,u.video_id,u.title,u.publish_at FROM youtube_uploads u
            JOIN renders r ON r.id=u.render_id WHERE r.project_id=?''', (project_id,)).fetchall()
    return [dict(row) for row in rows]


def upload(render_id, title, description, publish_at, made_for_kids, retry_acknowledged=False):
    with _lock:
        token = _token.get('access_token') if _token else None
    if not token: raise YouTubeError('Connect YouTube before uploading.')
    row = rendering.get(render_id)
    if not row or row['status'] != 'completed' or not row['path']:
        raise YouTubeError('Only completed renders can be uploaded.')
    try:
        from pathlib import Path
        directory = media.storage_root() / 'rendered' / str(row['project_id'])
        target = directory / (row['id'] + '.mp4')
        if directory.is_symlink() or target.is_symlink(): raise ValueError()
        expected = media.storage_path('rendered', row['project_id'], row['id'] + '.mp4')
        path = Path(row['path']).resolve()
        if path != expected or expected.parent != directory.resolve() or not path.is_file(): raise ValueError()
    except (ValueError, OSError): raise YouTubeError('Rendered video is not available.') from None
    with database.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT status FROM youtube_uploads WHERE render_id=?', (render_id,)).fetchone()
        if existing and (existing['status'] == 'uploading' or (existing['status'] != 'failed' and not retry_acknowledged)):
            raise YouTubeError('This render was already uploaded or its result is unknown. Check YouTube Studio before retrying.')
        db.execute('''INSERT INTO youtube_uploads(render_id,status,title,publish_at) VALUES(?, 'uploading', ?, ?)
            ON CONFLICT(render_id) DO UPDATE SET status='uploading',video_id=NULL,title=excluded.title,publish_at=excluded.publish_at,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')''', (render_id,title,publish_at))
    def update(state, video_id=None):
        with database.connect() as db:
            db.execute("UPDATE youtube_uploads SET status=?, video_id=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE render_id=?", (state,video_id,render_id))
    body = {'snippet': {'title': title, 'description': description}, 'status': {'privacyStatus':'private', 'publishAt':publish_at, 'selfDeclaredMadeForKids':made_for_kids}}
    try:
        size = path.stat().st_size
        if size <= 0: raise OSError()
    except OSError:
        update('failed'); raise YouTubeError('Rendered video is not available.') from None
    headers = {'Authorization':'Bearer '+token, 'X-Upload-Content-Type':'video/mp4', 'X-Upload-Content-Length':str(size)}
    try:
        response = httpx.post(UPLOAD_URL, headers=headers, json=body, timeout=30, follow_redirects=False)
        if response.status_code not in (200, 201):
            update('failed')
            raise YouTubeError(f'YouTube refused upload setup (HTTP {response.status_code}). Check your connection, quota and YouTube account.')
        location = response.headers.get('location', '')
        try:
            parts = urlsplit(location)
            valid = parts.scheme == 'https' and parts.hostname == 'www.googleapis.com' and not parts.username and not parts.password and parts.port in (None,443) and parts.path == '/upload/youtube/v3/videos' and not parts.fragment
        except ValueError: valid = False
        if not valid:
            update('failed'); raise YouTubeError('YouTube returned an invalid upload destination.')
    except httpx.HTTPError:
        update('failed'); raise YouTubeError('Could not start the upload. Check your connection and try again.') from None
    try:
        with path.open('rb') as f:
            response = httpx.put(location, headers={'Content-Type':'video/mp4', 'Content-Length':str(size), 'Authorization':'Bearer '+token}, content=iter(lambda: f.read(1024 * 1024), b''), timeout=httpx.Timeout(60, write=120), follow_redirects=False)
        if response.status_code not in (200, 201): raise ValueError()
        result = response.json(); video_id = result.get('id')
        if not isinstance(video_id, str) or len(video_id) != 11 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in video_id): raise ValueError()
    except (httpx.HTTPError, OSError, ValueError):
        update('unknown'); raise YouTubeError('Upload result is unknown. Check YouTube Studio before trying again to avoid duplicates.') from None
    # A successful upload does not by itself prove that YouTube accepted the schedule.
    returned_status = result.get('status') if isinstance(result.get('status'), dict) else {}
    try:
        actual = datetime.fromisoformat(returned_status.get('publishAt', '').replace('Z', '+00:00'))
        requested = datetime.fromisoformat(publish_at.replace('Z', '+00:00'))
        confirmed = returned_status.get('privacyStatus') == 'private' and actual.tzinfo is not None and actual.astimezone(timezone.utc) == requested.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError): confirmed = False
    state = 'uploaded' if confirmed else 'schedule_unconfirmed'
    update(state, video_id)
    return {'render_id':render_id, 'status':state, 'video_id':video_id, 'publish_at':publish_at}
