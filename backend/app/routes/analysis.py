"""Endpoints for locally transcribing and selecting suggested moments."""
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import database
from ..services import analysis, ingestion, metadata, shorts

router = APIRouter(prefix='/api')


class AnalysisRequest(BaseModel):
    model: str = 'base'
    duration_range: str = '30-45'
    force_transcribe: bool = False
    groq_titles: bool = False
    gemini_titles: bool = False
    cloud_titles: bool = False
    selection_mode: str = 'local'

    @field_validator('selection_mode')
    @classmethod
    def selection_supported(cls, value):
        if value not in {'local', 'llm'}: raise ValueError('Unsupported selection mode')
        return value

    @field_validator('model')
    @classmethod
    def model_supported(cls, value):
        if value not in {'tiny', 'base', 'small', 'medium'}: raise ValueError('Unsupported model')
        return value

    @field_validator('duration_range')
    @classmethod
    def valid_duration(cls, value):
        if value not in {'15-30', '30-45', '45-60', '60-90', '20-45', '20-60', '20-180'}: raise ValueError('Unsupported duration range')
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
    if body.selection_mode == 'local' and sum((body.groq_titles, body.gemini_titles, body.cloud_titles)) > 1:
        raise HTTPException(422, 'Choose only one title provider.')
    video = video_or_404(video_id)
    if not video['has_audio']:
        raise HTTPException(422, 'This video has no audio stream to transcribe.')
    if not video['duration'] or video['duration'] <= 0:
        raise HTTPException(422, 'Video duration is unavailable.')
    minimum, maximum = (int(x) for x in body.duration_range.split('-'))
    try:
        return safe_job(analysis.queue_job(video, body.model, minimum, maximum, body.force_transcribe, body.groq_titles, body.gemini_titles, body.cloud_titles, body.selection_mode))
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


class MetadataRequest(BaseModel):
    tone: Literal['curious','direct','funny'] = 'curious'
    language: str | None = Field(default=None, min_length=2, max_length=35, pattern=r'^[A-Za-z][A-Za-z0-9-]*$')
    cloud: bool = False  # Explicit opt-in to send text to providers.


@router.get('/clips/{clip_id}/metadata')
def get_metadata(clip_id: str):
    clip(clip_id)
    result=metadata.get(clip_id)
    if not result: raise HTTPException(404, 'No metadata generated for this clip')
    return result


@router.post('/clips/{clip_id}/metadata')
def create_metadata(clip_id: str, body: MetadataRequest):
    with database.connect() as db:
        row=db.execute('SELECT c.transcript,c.video_id,c.start,c.end,c.original_start,v.title FROM candidate_clips c JOIN videos v ON v.id=c.video_id WHERE c.id=?',(clip_id,)).fetchone()
    if not row: raise HTTPException(404, 'Clip not found')
    try:
        text=row['transcript']
        if row['original_start'] is not None:
            transcript=analysis.get_transcript(row['video_id']) or {}
            words=[w for segment in transcript.get('segments',[]) for w in segment.get('words',[]) if isinstance(w,dict)]
            from ..services import tighten
            text=' '.join(w['word'].strip() for w in tighten.remap_words(words,[(row['start'],row['end'])])).strip()
            if not text: raise ValueError('Timed transcript words are required to generate metadata for a trimmed clip.')
        result=metadata.generate(text,row['title'],body.tone,body.language,body.cloud)
        return metadata.save(clip_id,row['transcript'],row['title'],result,row['start'],row['end'])
    except ValueError as exc: raise HTTPException(409,str(exc)) from exc


class ClipUpdate(BaseModel):
    selected: bool | None = None
    start: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end: float | None = Field(default=None, ge=0, allow_inf_nan=False)


@router.patch('/clips/{clip_id}')
def select_clip(clip_id: str, body: ClipUpdate):
    if body.selected is None and body.start is None and body.end is None: raise HTTPException(422,'No clip changes supplied')
    with database.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT c.*,v.duration FROM candidate_clips c JOIN videos v ON v.id=c.video_id WHERE c.id=?',(clip_id,)).fetchone()
        if not row: raise HTTPException(404,'Clip not found')
        if (body.start is not None or body.end is not None) and db.execute("SELECT 1 FROM renders WHERE clip_id=? AND status IN ('pending','running')",(clip_id,)).fetchone(): raise HTTPException(409,'Cancel the active render before trimming.')
        start=row['start'] if body.start is None else body.start; end=row['end'] if body.end is None else body.end
        if body.start is not None or body.end is not None:
            if not row['duration'] or start<0 or end>row['duration'] or end-start<3 or end-start>shorts.MAX_DURATION: raise HTTPException(422,'Clip range must be within the source and between 3 and 180 seconds.')
            db.execute('UPDATE candidate_clips SET original_start=COALESCE(original_start,start),original_end=COALESCE(original_end,end),start=?,end=? WHERE id=?',(start,end,clip_id))
        if body.selected is not None: db.execute('UPDATE candidate_clips SET selected=? WHERE id=?',(int(body.selected),clip_id))
    return clip(clip_id)


@router.post('/clips/{clip_id}/revert')
def revert_clip(clip_id: str):
    with database.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM candidate_clips WHERE id=?',(clip_id,)).fetchone()
        if not row: raise HTTPException(404,'Clip not found')
        if db.execute("SELECT 1 FROM renders WHERE clip_id=? AND status IN ('pending','running')",(clip_id,)).fetchone(): raise HTTPException(409,'Cancel the active render before reverting.')
        if row['original_start'] is not None and row['original_end'] is not None:
            db.execute('UPDATE candidate_clips SET start=original_start,end=original_end,original_start=NULL,original_end=NULL WHERE id=?',(clip_id,))
    return clip(clip_id)


@router.delete('/clips/{clip_id}', status_code=204)
def delete_clip(clip_id: str):
    with database.connect() as db:
        cur = db.execute('DELETE FROM candidate_clips WHERE id=?', (clip_id,))
    if not cur.rowcount: raise HTTPException(404, 'Clip not found')
