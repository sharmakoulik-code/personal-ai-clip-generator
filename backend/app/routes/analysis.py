"""Endpoints for locally transcribing and selecting suggested moments."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import database
from ..services import analysis, ingestion

router = APIRouter(prefix='/api')


class AnalysisRequest(BaseModel):
    model: str = 'base'
    duration_range: str = '30-45'
    force_transcribe: bool = False

    @field_validator('model')
    @classmethod
    def model_supported(cls, value):
        if value not in {'tiny', 'base', 'small', 'medium'}: raise ValueError('Unsupported model')
        return value

    @field_validator('duration_range')
    @classmethod
    def valid_duration(cls, value):
        if value not in {'15-30', '30-45', '45-60', '60-90'}: raise ValueError('Unsupported duration range')
        return value


class Selection(BaseModel):
    selected: bool


def video_or_404(video_id: str) -> dict:
    video = ingestion.get_video(video_id)
    if not video: raise HTTPException(404, 'Video not found')
    return video


def safe_job(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != 'cancel_requested'}


@router.post('/videos/{video_id}/analyze', status_code=202)
def analyze(video_id: str, body: AnalysisRequest):
    video = video_or_404(video_id)
    if not video['has_audio']:
        raise HTTPException(422, 'This video has no audio stream to transcribe.')
    if not video['duration'] or video['duration'] <= 0:
        raise HTTPException(422, 'Video duration is unavailable.')
    minimum, maximum = (int(x) for x in body.duration_range.split('-'))
    try:
        return safe_job(analysis.queue_job(video, body.model, minimum, maximum, body.force_transcribe))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get('/videos/{video_id}/analysis-jobs')
def video_jobs(video_id: str):
    video_or_404(video_id)
    return [safe_job(j) for j in analysis.list_jobs(video_id)]


@router.get('/analysis/jobs/{job_id}')
def job(job_id: str):
    result = analysis.job_detail(job_id)
    if not result: raise HTTPException(404, 'Analysis job not found')
    return safe_job(result)


@router.post('/analysis/jobs/{job_id}/cancel')
def cancel(job_id: str):
    if not analysis.cancel(job_id): raise HTTPException(404, 'Analysis job not found')
    return safe_job(analysis.job_detail(job_id))


@router.get('/videos/{video_id}/transcription')
def transcript(video_id: str):
    video_or_404(video_id)
    result = analysis.get_transcript(video_id)
    if not result: raise HTTPException(404, 'No transcription available')
    return result


@router.get('/videos/{video_id}/clips')
def clips(video_id: str):
    video_or_404(video_id)
    return analysis.list_clips(video_id)


@router.patch('/videos/{video_id}/clips/selection')
def select_all(video_id: str, body: Selection):
    video_or_404(video_id)
    with database.connect() as db:
        db.execute('UPDATE candidate_clips SET selected=? WHERE video_id=?', (int(body.selected), video_id))
    return analysis.list_clips(video_id)


@router.get('/clips/{clip_id}')
def clip(clip_id: str):
    with database.connect() as db:
        row = db.execute('SELECT video_id FROM candidate_clips WHERE id=?', (clip_id,)).fetchone()
    if not row: raise HTTPException(404, 'Clip not found')
    return next(c for c in analysis.list_clips(row['video_id']) if c['id'] == clip_id)


@router.patch('/clips/{clip_id}')
def select_clip(clip_id: str, body: Selection):
    with database.connect() as db:
        cur = db.execute('UPDATE candidate_clips SET selected=? WHERE id=?', (int(body.selected), clip_id))
    if not cur.rowcount: raise HTTPException(404, 'Clip not found')
    return clip(clip_id)


@router.delete('/clips/{clip_id}', status_code=204)
def delete_clip(clip_id: str):
    with database.connect() as db:
        cur = db.execute('DELETE FROM candidate_clips WHERE id=?', (clip_id,))
    if not cur.rowcount: raise HTTPException(404, 'Clip not found')
