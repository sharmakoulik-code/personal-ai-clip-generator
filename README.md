# Personal AI Clip Generator

A local, single-user workspace. It contains a React/TypeScript/Tailwind dashboard, FastAPI and SQLite project history, and an **in-progress Phase 2 ingestion implementation**. No accounts, payment features, cloud storage or publishing are included. AI, transcription, captions and clip rendering are not implemented.

Phase 2 supports streamed local video uploads, YouTube metadata and download jobs through yt-dlp, FFprobe validation, local storage, job polling, cancellation, and project/video deletion. Only import videos you own or have permission to download; do not bypass access controls. The YouTube download and Windows end-to-end paths still require validation on a Windows computer with a permitted YouTube video.

## Windows requirements

- Python 3.11+ with the `py` launcher; Node.js 22.12+ on 22.x, 24.x, or 26+; Git.
- **FFmpeg and FFprobe** on PATH, required for video validation, thumbnails, and merging separate video/audio streams. One option on Windows is `winget install --id Gyan.FFmpeg -e`. Reopen PowerShell afterward, then verify `ffmpeg -version` and `ffprobe -version`.
- Network access is needed for YouTube metadata and downloads. YouTube availability and formats can change.

## Start locally on Windows

From the project root:

```powershell
powershell -ExecutionPolicy Bypass -File .\start-local.ps1
```

The script installs dependencies and opens two windows. Open **http://127.0.0.1:5173**. Press Ctrl+C in each window to stop. The default startup commands bind FastAPI and Vite to 127.0.0.1 only. They have no authentication: do not expose them to a network. The Arena test environment may use different, temporary network bindings.

Manual startup uses two terminals from the repository root:

```powershell
# Terminal 1
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```powershell
# Terminal 2
cd frontend
npm ci
npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

## Ingest a video

Create a project and select it in the source section. For a local video, choose or drop an MP4, MOV, MKV, WEBM or AVI. The browser shows actual bytes sent; the job then validates the file with FFprobe. For YouTube, paste a supported HTTPS video URL, fetch its metadata, review it and select a quality, then confirm. Download percentages reflect the current stream, not necessarily the entire multi-stream merge. Thumbnail generation may be unavailable if FFmpeg cannot extract a frame. Cancel pending or active jobs using their Cancel button. A cancellation during a network operation may take until the next progress callback or network timeout.

The default upload limit is 10 GiB; set `CLIP_MAX_UPLOAD_BYTES` in the backend terminal to change it. Data is stored in `storage/` (ignored by Git) and SQLite in `backend/data/clips.sqlite3`. Set `CLIP_STORAGE_PATH` and `CLIP_DB_PATH` before starting the backend to change these locations. Back up both database and storage together. Individual videos and entire projects can be deleted from the dashboard; project deletion also deletes associated files. Active jobs must be cancelled first. Jobs and videos persist after restart; transfers interrupted during shutdown are marked failed and must be retried. Keep one backend process running: the local queue is designed for a single worker.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
cd backend
..\.venv\Scripts\python.exe -m pytest -q
cd ..\frontend
npm ci
npm test
npm run build
```

The video tests use real FFmpeg/FFprobe when available; YouTube tests use controlled mocks, not a real YouTube connection. Review dependency advisories before deployment; this app is for local personal use only.
