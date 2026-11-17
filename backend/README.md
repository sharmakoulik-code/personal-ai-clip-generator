# Backend

The FastAPI API stores project, ingestion job, and video metadata in SQLite. Large files go into local storage, not the database. The default server command is:

```powershell
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Install the dependencies in `requirements.txt` first. FFmpeg and FFprobe must be on PATH for video processing. `CLIP_DB_PATH`, `CLIP_STORAGE_PATH`, `CLIP_MAX_UPLOAD_BYTES`, `CLIP_FFMPEG` and `CLIP_FFPROBE` are optional backend-only settings. The API has no authentication: never expose it to an untrusted network. See the root README for Windows setup and current Phase 2 validation limitations.
