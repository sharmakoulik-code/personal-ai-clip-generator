"""Render jobs and safe local video previews."""
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .. import database
from ..services import analysis, captions, ingestion, media, rendering

router = APIRouter(prefix='/api')

class RenderSettings(BaseModel):
    layout: Literal['smart','center','blur','split','auto','follow','fit_blur','stack','letterbox'] = 'follow'
    resolution: int = 1920
    preset: Literal['legacy', 'shorts'] = 'legacy'
    tighten: bool = True
    loop_polish: bool = False
    start_offset: float = Field(default=0, ge=-5, le=5, allow_inf_nan=False)
    end_offset: float = Field(default=0, ge=-5, le=5, allow_inf_nan=False)
    x_offset: float = Field(default=0, ge=-.35, le=.35, allow_inf_nan=False)
    y_offset: float = Field(default=0, ge=-.35, le=.35, allow_inf_nan=False)
    trim_fillers: bool = True
    max_pause: float = Field(default=.6, ge=.08, le=5, allow_inf_nan=False)
    loudness_lufs: float = Field(default=-14, ge=-30, le=-5, allow_inf_nan=False)

    quality: Literal['high','balanced','small'] = 'balanced'
    horizontal: float = Field(default=0, ge=-1, le=1, allow_inf_nan=False)
    vertical: float = Field(default=0, ge=-1, le=1, allow_inf_nan=False)
    zoom: float = Field(default=1, ge=1, le=2, allow_inf_nan=False)
    detect_interval: float = Field(default=1, ge=.5, le=5, allow_inf_nan=False)
    track_faces: bool = True
    tracking_engine: Literal['standard', 'subject'] = 'standard'
    active_speaker: bool = False
    captions: bool = False
    caption_safe_zone: bool = True
    force_captions: bool = False
    caption_style: Literal['highlight', 'classic', 'minimal'] = 'highlight'
    caption_uppercase: bool = False
    caption_position: Literal['on_video','safe_zone'] | None = None
    letterbox_bg: str = Field(default='#000000', pattern=r'^#[0-9A-Fa-f]{6}$')
    letterbox_vertical_bias: float = Field(default=.02, ge=-.15, le=.15, allow_inf_nan=False)
    letterbox_edge: bool = False
    pad: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    normalize_audio: bool = False
    smooth_time: float = Field(default=.55, ge=.15, le=2, allow_inf_nan=False)
    dead_zone: float = Field(default=.12, ge=0, le=.5, allow_inf_nan=False)
    stack_two_speakers: bool = False
    fit_vertical_bias: float = Field(default=-.05, ge=-.25, le=.25, allow_inf_nan=False)
    sharpen: float = Field(default=.35, ge=0, le=1, allow_inf_nan=False)
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


@router.get('/shorts/constants')
def shorts_constants():
    from ..services import shorts
    return {'max_duration':shorts.MAX_DURATION,'default_min_seconds':shorts.DEFAULT_MIN_SECONDS,'default_max_seconds':shorts.DEFAULT_MAX_SECONDS,'width':shorts.RECOMMENDED_WIDTH,'height':shorts.RECOMMENDED_HEIGHT,'minimum_width':shorts.MIN_WIDTH,'minimum_height':shorts.MIN_HEIGHT,'target_lufs':shorts.TARGET_LUFS,'true_peak':shorts.TARGET_TRUE_PEAK,'max_title':shorts.MAX_TITLE,'max_description':shorts.MAX_DESCRIPTION,'min_hashtags':shorts.MIN_HASHTAGS,'max_hashtags':shorts.MAX_HASHTAGS,'container':shorts.CONTAINER,'video_codec':shorts.VIDEO_CODEC,'audio_codec':shorts.AUDIO_CODEC,'pixel_format':shorts.PIXEL_FORMAT,'colour_space':shorts.COLOUR_SPACE,'fps_options':shorts.FPS_OPTIONS,'official_min_duration':shorts.OFFICIAL_MIN_DURATION,'safe_zone':shorts.SAFE_ZONE,'recheck':'Verify current YouTube requirements before publishing.'}


@router.get('/clips/{clip_id}/preview-path')
def preview_path(clip_id: str, settings: RenderSettings = Depends()):
    validate_settings(settings)
    clip=clip_or_404(clip_id)
    try: return rendering.preview_path(clip,settings.model_dump())
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc


@router.get('/clips/{clip_id}/render-preview')
def render_preview(clip_id: str, settings: RenderSettings = Depends()):
    validate_settings(settings)
    clip = clip_or_404(clip_id)
    try: image, status = rendering.preview(clip, settings.model_dump())
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc
    return Response(image, media_type='image/jpeg', headers={'X-Framing-Status': status, 'Cache-Control':'no-store'})


def caption_content(clip_id, render_id, kind):
    clip = clip_or_404(clip_id)
    if render_id:
        row = render_or_404(render_id)
        if row['clip_id'] != clip_id or row['status'] != 'completed':
            raise HTTPException(404, 'Captions for this render are not available')
        if row.get('caption_words_json') is None:
            raise HTTPException(409, 'This older render has no saved caption timeline; render it again.')
        try: words = json.loads(row['caption_words_json'])
        except (TypeError, ValueError): raise HTTPException(409, 'Saved caption timeline is invalid.') from None
        start=0
        info=json.loads(row.get('info_json') or '{}')
        end=info.get('duration', row['end']-row['start'])
    else:
        transcript = analysis.get_transcript(clip['video_id']) or {}
        words = [word for segment in transcript.get('segments', []) for word in segment.get('words', [])]
        start,end=clip['start'],clip['end']
    try:
        content = captions.build_vtt(words,start,end) if kind=='vtt' else captions.build_srt(words,start,end)
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc
    media_type='text/vtt' if kind=='vtt' else 'application/x-subrip'
    return Response(content, media_type=media_type+'; charset=utf-8', headers={'Content-Disposition':f'attachment; filename="captions.{kind}"'})


@router.get('/clips/{clip_id}/captions.srt')
def caption_srt(clip_id: str, render_id: str | None = None):
    return caption_content(clip_id,render_id,'srt')


@router.get('/clips/{clip_id}/captions.vtt')
def caption_vtt(clip_id: str, render_id: str | None = None):
    return caption_content(clip_id,render_id,'vtt')


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
                if field == 'path':
                    p.with_suffix('.txt').unlink(missing_ok=True)
                    p.with_suffix('.metadata.json').unlink(missing_ok=True)
        db.execute('DELETE FROM renders WHERE id=?', (rid,))
