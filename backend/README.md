# Backend

The FastAPI API stores project, ingestion job, and video metadata in SQLite. Large files go into local storage, not the database. The default server command is:

```powershell
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Install the dependencies in `requirements.txt` first. FFmpeg and FFprobe must be on PATH for video processing. `CLIP_DB_PATH`, `CLIP_STORAGE_PATH`, `CLIP_MAX_UPLOAD_BYTES`, `CLIP_FFMPEG` and `CLIP_FFPROBE` are optional backend-only settings. The API has no authentication: never expose it to an untrusted network. See the root README for Windows setup and validation limitations.

## Phase 3 analysis API and storage

Install the existing `requirements.txt` (Python 3.11+); it includes `faster-whisper==1.2.1`. FFmpeg and FFprobe are required as for ingestion. Default inference is CPU/int8, with optional `CLIP_WHISPER_DEVICE=cpu|cuda|auto`. Models are downloaded to `<CLIP_STORAGE_PATH>/models` or loaded from `models/whisper-{tiny,base,small,medium}`. CUDA requires a separately compatible NVIDIA runtime. See the root README for Windows use, downloading models, limitations and validation status.

- `POST /api/videos/{video_id}/analyze`: body `{ "model": "base", "duration_range": "15-30", "force_transcribe": false, "groq_titles": false, "gemini_titles": false, "cloud_titles": false }` (other ranges: `30-45`, `45-60`, `60-90`). Returns 202; at most one queued/running analysis per video.
- `GET /api/videos/{video_id}/analysis-jobs`, `GET /api/analysis/jobs/{id}`, `POST /api/analysis/jobs/{id}/cancel`: persistent job progress/state and cancellation.
- `GET /api/videos/{video_id}/transcription`: stored language, model, timed segments and word timestamps; 404 until available.
- `GET /api/videos/{video_id}/clips`: candidates with timestamps, title, hook, transcript, weighted scores and selection. `PATCH /api/clips/{id}` with `{ "selected": true }` updates one; `PATCH /api/videos/{video_id}/clips/selection` updates all.

`transcripts` stores the cached model/language and video link, `transcript_segments` stores text/timestamps and serialized word timestamps, `candidate_clips` stores suggested ranges/score breakdowns/selections, and `analysis_jobs` stores the queue, parameters, progress and failures. Foreign keys cascade on deletion; a partial unique index prevents duplicate active jobs per video. The single in-process worker marks interrupted running jobs failed on application startup and resumes queued jobs. Do not start multiple backend processes against the same storage. Results are atomically replaced only after successful analysis. With no recognized speech the job reports failure; videos with no audio stream are rejected before enqueue.

For optional real inference testing, set `CLIP_TEST_MODEL_DIR` to a converted Base model folder and `CLIP_TEST_SPEECH_VIDEO` to the JFK speech MP4 fixture before running `python -m pytest`. Without these, the real-model test is skipped. Normal model download from Hugging Face and CUDA remain environment-dependent and are not proven by this optional test.


`cloud_titles: true` opts into Cloud AI: try Groq first, then Google Gemini on a provider failure or missing key. Configure `GROQ_API_KEY` and `GEMINI_API_KEY` in the backend process to enable both. It sends candidate transcript excerpts to the selected provider(s), never the video; failures preserve prior results. `groq_titles` and `gemini_titles` remain available for older API clients to select a single provider. At most one of the three flags may be true. Startup adds the three flag columns to existing `analysis_jobs` tables. See the root README for Windows setup, privacy and limitations.


## Phase 4 rendering API

`opencv-python-headless` provides the bundled CPU Haar detector; FFmpeg/FFprobe must be on PATH and the FFmpeg build must support libx264 and AAC. Optional `CLIP_RENDER_ENCODER=nvenc` requests NVIDIA H.264 encoding without automatic fallback; default is `cpu`. This encoder and Phase 4 on Windows have not been validated. The detector only frames visible faces and cannot identify an active speaker.

- `GET /api/videos/{id}/stream`: local source with HTTP range support.
- `GET /api/clips/{id}/render-preview`: representative JPEG at clip start, with `X-Framing-Status`; settings are query parameters.
- `POST /api/clips/{id}/render` or `POST /api/projects/{id}/render-batch`: 202 and persistent jobs. Batch includes selected clips across the project and accepts at most 100. Settings JSON: `{ "layout": "smart", "resolution": 1920, "quality": "balanced", "horizontal": 0, "vertical": 0, "zoom": 1, "track_faces": true, "detect_interval": 1, "split_ratio": 0.67 }`. Layout: `smart|center|blur|split`; resolution: `1280|1920` (portrait height); quality: `high|balanced|small`; offsets -1..1, zoom 1..2, interval .5..5 seconds, split ratio .5.. .8.
- `GET /api/renders?project_id={id}` or `?video_id={id}` and `GET /api/renders/{id}`: newest jobs and details (list limited to 200).
- `POST /api/renders/{id}/cancel`, `POST /api/renders/{id}/retry`, `DELETE /api/renders/{id}`; `GET /api/renders/{id}/stream` and `/download` for completed jobs.

The `renders` table snapshots clip ranges/settings and tracks progress and output paths; `face_track_cache` stores sampled crop centers. A partial unique index blocks two active renders for a clip. A single worker processes jobs, uses bounded-frame sampling and FFmpeg, validates output and moves it into local rendered storage. The first audio stream is retained if present. Pending work resumes after restart; interrupted jobs are marked failed, and cancelled/failed jobs can be retried if their source clip still exists. Video/project deletion and re-analysis are blocked by active renders. Source files and outputs are restricted to local storage paths. See root README for operational and Windows test instructions and limitations.
