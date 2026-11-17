# Backend

The FastAPI API stores project, ingestion job, and video metadata in SQLite. Large files go into local storage, not the database. The default server command is:

```powershell
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Install the dependencies in `requirements.txt` first. FFmpeg and FFprobe must be on PATH for video processing. `CLIP_DB_PATH`, `CLIP_STORAGE_PATH`, `CLIP_MAX_UPLOAD_BYTES`, `CLIP_FFMPEG` and `CLIP_FFPROBE` are optional backend-only settings. The API has no authentication: never expose it to an untrusted network. See the root README for Windows setup and current Phase 2 validation limitations.

## Phase 3 analysis API and storage

Install the existing `requirements.txt` (Python 3.11+); it includes `faster-whisper==1.2.1`. FFmpeg and FFprobe are required as for ingestion. Default inference is CPU/int8, with optional `CLIP_WHISPER_DEVICE=cpu|cuda|auto`. Models are downloaded to `<CLIP_STORAGE_PATH>/models` or loaded from `models/whisper-{tiny,base,small,medium}`. CUDA requires a separately compatible NVIDIA runtime. See the root README for Windows use, downloading models, limitations and validation status.

- `POST /api/videos/{video_id}/analyze`: body `{ "model": "base", "duration_range": "15-30", "force_transcribe": false }` (other ranges: `30-45`, `45-60`, `60-90`). Returns 202; at most one queued/running analysis per video.
- `GET /api/videos/{video_id}/analysis-jobs`, `GET /api/analysis/jobs/{id}`, `POST /api/analysis/jobs/{id}/cancel`: persistent job progress/state and cancellation.
- `GET /api/videos/{video_id}/transcription`: stored language, model, timed segments and word timestamps; 404 until available.
- `GET /api/videos/{video_id}/clips`: candidates with timestamps, title, hook, transcript, weighted scores and selection. `PATCH /api/clips/{id}` with `{ "selected": true }` updates one; `PATCH /api/videos/{video_id}/clips/selection` updates all.

`transcripts` stores the cached model/language and video link, `transcript_segments` stores text/timestamps and serialized word timestamps, `candidate_clips` stores suggested ranges/score breakdowns/selections, and `analysis_jobs` stores the queue, parameters, progress and failures. Foreign keys cascade on deletion; a partial unique index prevents duplicate active jobs per video. The single in-process worker marks interrupted running jobs failed on application startup and resumes queued jobs. Do not start multiple backend processes against the same storage. Results are atomically replaced only after successful analysis. With no recognized speech the job reports failure; videos with no audio stream are rejected before enqueue.

For optional real inference testing, set `CLIP_TEST_MODEL_DIR` to a converted Base model folder and `CLIP_TEST_SPEECH_VIDEO` to the JFK speech MP4 fixture before running `python -m pytest`. Without these, the real-model test is skipped. Normal model download from Hugging Face, Windows and CUDA remain environment-dependent and are not proven by this optional test.
