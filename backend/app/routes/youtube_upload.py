"""Explicit, local-only YouTube authorization and rendered-video uploads."""
from datetime import datetime, timezone
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from ..services import youtube_upload

router = APIRouter(prefix='/api/youtube', tags=['youtube upload'])


def local_browser(request: Request):
    # Prevent web pages at other origins from initiating uploads through a local backend.
    host = request.headers.get('host', '').lower()
    if host not in ('127.0.0.1:5173','localhost:5173','127.0.0.1:8000','localhost:8000'):
        raise HTTPException(403, 'YouTube upload is available only from the local app.')
    origin = request.headers.get('origin')
    allowed_origin = origin in ('http://127.0.0.1:5173','http://localhost:5173')
    if (not allowed_origin and not (request.method == 'GET' and origin is None)) or request.headers.get('x-clip-studio') != 'youtube-upload':
        raise HTTPException(403, 'YouTube upload is available only from the local app.')


class UploadRequest(BaseModel):
    render_id: Annotated[str, Field(min_length=1, max_length=100)]
    title: Annotated[str, Field(min_length=1, max_length=100)]
    description: Annotated[str, Field(max_length=5000)] = ''
    publish_at: datetime
    made_for_kids: bool
    retry_acknowledged: bool = False

    @field_validator('title')
    @classmethod
    def title_not_blank(cls, value):
        if not value.strip() or '<' in value or '>' in value: raise ValueError('Enter a title without angle brackets.')
        return value.strip()

    @field_validator('description')
    @classmethod
    def description_valid(cls, value):
        if '<' in value or '>' in value: raise ValueError('Description must not contain angle brackets.')
        return value

    @field_validator('publish_at')
    @classmethod
    def future_schedule(cls, value):
        if value.tzinfo is None or value.utcoffset() is None or value <= datetime.now(timezone.utc):
            raise ValueError('Choose a future publication time including a timezone.')
        return value.astimezone(timezone.utc)


@router.get('/status', dependencies=[Depends(local_browser)])
def status(): return youtube_upload.status()


@router.post('/connect', dependencies=[Depends(local_browser)])
def connect():
    try: return {'url':youtube_upload.connect()}
    except youtube_upload.YouTubeError as exc: raise HTTPException(409, str(exc)) from None


@router.post('/disconnect', dependencies=[Depends(local_browser)])
def disconnect():
    youtube_upload.disconnect()
    return {'connected':False}


@router.get('/uploads', dependencies=[Depends(local_browser)])
def uploads(project_id: int): return youtube_upload.history(project_id)


@router.post('/upload', dependencies=[Depends(local_browser)])
def upload(body: UploadRequest):
    try:
        return youtube_upload.upload(body.render_id, body.title, body.description,
            body.publish_at.isoformat().replace('+00:00', 'Z'), body.made_for_kids, body.retry_acknowledged)
    except youtube_upload.YouTubeError as exc: raise HTTPException(409, str(exc)) from None
