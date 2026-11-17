"""Render jobs and safe local video previews."""
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .. import database
from ..services import ingestion, media, rendering

router = APIRouter(prefix='/api')

class RenderSettings(BaseModel):
    layout: Literal['smart','center','blur','split'] = 'smart'
    resolution: int = 1920

    quality: Literal['high','balanced','small'] = 'balanced'
    horizontal: float = Field(default=0, ge=-1, le=1, allow_inf_nan=False)
    vertical: float = Field(default=0, ge=-1, le=1, allow_inf_nan=False)
    zoom: float = Field(default=1, ge=1, le=2, allow_inf_nan=False)
    detect_interval: float = Field(default=1, ge=.5, le=5, allow_inf_nan=False)
    track_faces: bool = True
    split_ratio: float = Field(default=.67, ge=.5, le=.8, allow_inf_nan=False)


def validate_settings(settings):
    if settings.resolution not in (1280, 1920):
        raise HTTPException(422, 'Choose 720 or 1080 portrait resolution.')


def clip_or_404(clip_id):
    with database.connect() as db: row = db.execute('SELECT * FROM candidate_clips WHERE id=?', (clip_id,)).fetchone()
    if not row: raise HTTPException(404, 'Clip not found')
    return dict(row)


def render_or_404(rid):
    row = rendering.get(rid)
    if not row: raise HTTPException(404, 'Render not found')
    return row


@router.get('/videos/{video_id}/stream')
def source_stream(video_id: str):
    try: video, path = rendering.source_video(video_id)
    except ValueError as exc: raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type='video/mp4' if path.suffix.lower() == '.mp4' else None, content_disposition_type='inline')


@router.get('/clips/{clip_id}/render-preview')
def render_preview(clip_id: str, settings: RenderSettings = Depends()):
    validate_settings(settings)
    clip = clip_or_404(clip_id)
    try: image, status = rendering.preview(clip, settings.model_dump())
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc
    return Response(image, media_type='image/jpeg', headers={'X-Framing-Status': status, 'Cache-Control':'no-store'})


@router.post('/clips/{clip_id}/render', status_code=202)
def render_clip(clip_id: str, settings: RenderSettings):
    validate_settings(settings)
    clip_or_404(clip_id)
    try:
        media.require_tools('ffmpeg', 'ffprobe')
        return rendering.queue_clips([clip_id], settings.model_dump())[0]
    except ValueError as exc: raise HTTPException(409, str(exc)) from exc


@router.post('/projects/{project_id}/render-batch', status_code=202)
def render_batch(project_id: int, settings: RenderSettings):
    validate_settings(settings)
    if not ingestion.project_exists(project_id): raise HTTPException(404, 'Project not found')
    with database.connect() as db:
        ids = [r['id'] for r in db.execute('SELECT c.id FROM candidate_clips c JOIN videos v ON v.id=c.video_id WHERE v.project_id=? AND c.selected=1 ORDER BY c.video_id,c.start', (project_id,))]
    if not ids: raise HTTPException(422, 'Select at least one clip to render.')
    if len(ids) > 100: raise HTTPException(422, 'Select no more than 100 clips per batch.')
    try:
        media.require_tools('ffmpeg', 'ffprobe')
        return rendering.queue_clips(ids, settings.model_dump())
    except ValueError as exc: raise HTTPException(409, str(exc)) from exc


@router.get('/renders')
def renders(video_id: str | None = None, project_id: int | None = None): return rendering.list_renders(video_id, project_id)


@router.get('/renders/{rid}')
def render_detail(rid: str): return rendering.public(render_or_404(rid))


@router.post('/renders/{rid}/cancel')
def cancel_render(rid: str):
    if not rendering.cancel(rid): raise HTTPException(404, 'Render not found')
    return rendering.public(rendering.get(rid))


@router.post('/renders/{rid}/retry', status_code=202)
def retry_render(rid: str):
    row = render_or_404(rid)
    if row['status'] not in ('failed','cancelled'): raise HTTPException(409, 'Only failed or cancelled renders can be retried.')
    try: return rendering.queue_clips([row['clip_id']], json.loads(row['settings_json']))[0]
    except ValueError as exc: raise HTTPException(409, str(exc)) from exc


def output_path(row):
    if row['status'] != 'completed' or not row['path']: raise HTTPException(404, 'Rendered video is not available')
    path = Path(row['path']).resolve()
    root = media.storage_root() / 'rendered' / str(row['project_id'])
    if not path.is_relative_to(root) or not path.is_file(): raise HTTPException(404, 'Rendered video is not available')
    return path


@router.get('/renders/{rid}/stream')
def render_stream(rid: str): return FileResponse(output_path(render_or_404(rid)), media_type='video/mp4', content_disposition_type='inline')


@router.get('/renders/{rid}/download')
def download(rid: str): return FileResponse(output_path(render_or_404(rid)), media_type='video/mp4', filename='clip-' + rid + '.mp4')


@router.delete('/renders/{rid}', status_code=204)
def delete_render(rid: str):
    with database.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM renders WHERE id=?', (rid,)).fetchone()
        if not row: raise HTTPException(404, 'Render not found')
        if row['status'] in ('pending','running'): raise HTTPException(409, 'Cancel the active render first.')
        for field in ('path','thumbnail_path'):
            if row[field]:
                p = Path(row[field]).resolve()
                if not p.is_relative_to(media.storage_root() / 'rendered' / str(row['project_id'])): raise HTTPException(500, 'Invalid render path')
                p.unlink(missing_ok=True)
        db.execute('DELETE FROM renders WHERE id=?', (rid,))
