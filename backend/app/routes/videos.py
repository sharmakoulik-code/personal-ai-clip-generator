"""Local video ingestion and persisted job endpoints."""
import os
from pathlib import Path, PureWindowsPath

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from starlette.requests import ClientDisconnect

from ..services import ingestion, media

router = APIRouter(prefix='/api')


class YouTubeURL(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class YouTubeDownload(YouTubeURL):
    project_id: int = Field(gt=0)
    quality: str = '1080'
    title: str | None = Field(default=None, max_length=200)

    @field_validator('quality')
    @classmethod
    def valid_quality(cls, value):
        if value not in {'720', '1080', 'best'}:
            raise ValueError('Choose 720, 1080, or best')
        return value


class UploadCreate(BaseModel):
    project_id: int = Field(gt=0)
    filename: str = Field(min_length=1, max_length=200)
    size: int = Field(gt=0)

    @field_validator('filename')
    @classmethod
    def safe_filename(cls, value):
        if Path(value).name != value or PureWindowsPath(value).name != value or value in {'.', '..'} or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid filename')
        if Path(value).suffix.lower() not in media.SUPPORTED_EXTENSIONS:
            raise ValueError('Supported formats: MP4, MOV, MKV, WEBM, AVI')
        return value


def public_job(job: dict) -> dict:
    return {k: v for k, v in job.items() if k not in {'temp_path', 'cancel_requested'}}


def public_video(video: dict) -> dict:
    return {k: v for k, v in video.items() if k not in {'path', 'thumbnail_path'}} | {'has_thumbnail': bool(video['thumbnail_path'])}


def max_upload_bytes() -> int:
    return int(os.environ.get('CLIP_MAX_UPLOAD_BYTES', str(10 * 1024**3)))


def check_project(project_id: int) -> None:
    if not ingestion.project_exists(project_id):
        raise HTTPException(404, 'Project not found')


@router.post('/videos/youtube/metadata')
def youtube_metadata(body: YouTubeURL):
    try:
        return media.youtube_metadata(body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/videos/youtube/download', status_code=202)
def youtube_download(body: YouTubeDownload):
    check_project(body.project_id)
    try:
        url, _ = media.canonical_youtube_url(body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        media.require_tools('ffmpeg', 'ffprobe')
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc
    return public_job(ingestion.create_job(body.project_id, 'youtube', url, body.quality, body.title))


@router.post('/videos/upload', status_code=201)
def create_upload(body: UploadCreate):
    check_project(body.project_id)
    if body.size > max_upload_bytes():
        raise HTTPException(413, f'File exceeds the configured upload limit ({max_upload_bytes()} bytes).')
    try:
        media.require_tools('ffprobe')
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc
    return public_job(ingestion.create_job(body.project_id, 'upload', body.filename, title=body.filename))


@router.put('/jobs/{job_id}/upload', status_code=202)
async def upload_bytes(job_id: str, request: Request):
    job = ingestion.get_job(job_id)
    if not job or job['kind'] != 'upload':
        raise HTTPException(404, 'Upload job not found')
    if job['status'] != 'uploading' or job['temp_path']:
        raise HTTPException(409, 'This upload has already started or is no longer active')
    length = request.headers.get('content-length')
    if length and (not length.isdigit() or int(length) > max_upload_bytes()):
        ingestion.finish(job_id, 'failed', 'File exceeds the configured upload limit.')
        raise HTTPException(413, 'File exceeds the configured upload limit.')
    target = media.storage_path('temp', job['project_id'], job_id + Path(job['source']).suffix.lower())
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic claim prevents two PUTs from writing the same job.
    with ingestion.database.connect() as db:
        claim = db.execute("UPDATE jobs SET temp_path=? WHERE id=? AND status='uploading' AND temp_path IS NULL", (str(target), job_id))
        if not claim.rowcount:
            raise HTTPException(409, 'This upload has already started')
    received = 0
    try:
        with target.open('xb') as output:
            async for chunk in request.stream():
                ingestion.check_cancel(job_id)
                received += len(chunk)
                if received > max_upload_bytes():
                    raise HTTPException(413, 'File exceeds the configured upload limit.')
                output.write(chunk)
                pct = min(100, received * 100 / int(length)) if length and int(length) else None
                ingestion.update_job(job_id, progress=pct)
        if not received or (length and received != int(length)):
            raise ValueError('Upload was empty or incomplete.')
        ingestion.check_cancel(job_id)
        ingestion.update_job(job_id, status='pending', progress=None)
        return public_job(ingestion.get_job(job_id))
    except ingestion.Cancelled:
        target.unlink(missing_ok=True)
        ingestion.finish(job_id, 'cancelled', 'Upload cancelled.')
        raise HTTPException(409, 'Upload cancelled')
    except (ClientDisconnect, HTTPException, OSError, ValueError) as exc:
        target.unlink(missing_ok=True)
        current = ingestion.get_job(job_id)
        if current and current['cancel_requested']:
            ingestion.finish(job_id, 'cancelled', 'Upload cancelled.')
        else:
            ingestion.finish(job_id, 'failed', 'Upload was interrupted, too large, or could not be saved.')
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(400, 'Upload failed or was interrupted.') from exc


@router.get('/jobs')
def jobs():
    return [public_job(job) for job in ingestion.list_jobs()]


@router.get('/jobs/{job_id}')
def job_detail(job_id: str):
    job = ingestion.get_job(job_id)
    if not job:
        raise HTTPException(404, 'Job not found')
    return public_job(job)


@router.post('/jobs/{job_id}/cancel')
def cancel(job_id: str):
    if not ingestion.cancel_job(job_id):
        raise HTTPException(404, 'Job not found')
    return public_job(ingestion.get_job(job_id))


@router.get('/projects/{project_id}/videos')
def videos(project_id: int):
    check_project(project_id)
    return [public_video(v) for v in ingestion.list_videos(project_id)]


@router.get('/videos/{video_id}/thumbnail')
def thumbnail(video_id: str):
    video = ingestion.get_video(video_id)
    if not video or not video['thumbnail_path']:
        raise HTTPException(404, 'Thumbnail not found')
    path = Path(video['thumbnail_path']).resolve()
    if not path.is_relative_to(media.storage_root()) or not path.is_file():
        raise HTTPException(404, 'Thumbnail not found')
    return FileResponse(path, media_type='image/jpeg')


@router.delete('/videos/{video_id}', status_code=204)
def delete_video(video_id: str):
    try:
        deleted = ingestion.delete_video(video_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(500, 'Could not remove video files; check file permissions and try again.') from exc
    if not deleted:
        raise HTTPException(404, 'Video not found')
