# Personal AI Clip Generator

A local, single-user workspace. It contains a React/TypeScript/Tailwind dashboard, FastAPI and SQLite project history, video ingestion, and an **in-progress Phase 3 analysis implementation**. No accounts, payment features, cloud storage or publishing are included. Clip rendering and captions are not implemented.

Phase 2 supports streamed local video uploads, YouTube metadata and download jobs through yt-dlp, FFprobe validation, local storage, job polling, cancellation, and project/video deletion. Only import videos you own or have permission to download; do not bypass access controls. Local upload, a YouTube download, persistence, cancellation, and deletion were tested by the user on Windows; Phase 3 still needs Windows validation.

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

## Phase 3: local speech analysis (Windows validation pending)

After importing a video with an audio track, select its project and choose **Analyze Video with AI** next to the video. Select a model and clip-duration range, then start analysis. The worker extracts bounded 60-second audio windows with a small overlap, runs faster-whisper with word timestamps, stores the transcript in SQLite and ranks suggestions and suppresses heavily overlapping results. Stages and transcription progress come from actual work; the initial model download has no percentage. Cancellation is checked between extraction/transcription steps, so a model download or inference operation may take time to stop.

Whisper Base is the default; Tiny is faster, while Small and Medium need more memory and time. The first run downloads the chosen CTranslate2 model from Hugging Face into `storage/models/`; subsequent runs reuse the cached model. An internet connection and disk space are needed for the first download; no paid API is required. For an offline installation, place a compatible converted model under `storage/models/whisper-tiny`, `whisper-base`, `whisper-small`, or `whisper-medium` including `model.bin`, `config.json`, tokenizer and vocabulary files. Models and media are ignored by Git. A suitable Python environment installs the `faster-whisper` dependency when the startup script runs.

The default device is CPU (int8). To try CUDA on a compatible NVIDIA system, set `$env:CLIP_WHISPER_DEVICE = 'cuda'` in the PowerShell window **before** launching the startup script; CUDA and CTranslate2's required GPU libraries must already be installed. `'auto'` attempts GPU detection and otherwise uses CPU; `'cpu'` forces CPU. GPU operation is not yet validated on Windows.

The score is a **local heuristic**, not a virality prediction or semantic LLM assessment. It combines lexical signals, punctuation, sentence/clause boundaries, and standalone-context indicators. Keyword signals are primarily English; multilingual speech is supported by Whisper, but ranking in other languages is limited. By default, titles and hooks are literal transcript excerpts; optional Groq titles are generated and must be checked. Whisper can mistranscribe or hallucinate; zero-duration word timestamps are excluded, and clips may be unavailable if no spoken span fits the chosen range. Review the transcript, boundaries, and score before using any suggestion. Audio decoding is chunked, but transcript data grows with video length; ranking keeps a bounded pool of 3,000 windows and shows at most 30 suggestions. This cap can omit useful moments from very long videos. The optional Groq feature described below uses an LLM for titles and explanations only; no scene detection, clip rendering or captions are implemented.

Existing transcripts are reused when the same model is selected. To obtain a fresh transcription, check **Re-transcribe even if cached**. Re-running analysis keeps old results until the new run succeeds, then replaces the previous clips and their selections. Individual selections and transcripts persist across restarts. Deleting a video or project removes its associated transcript, analysis jobs and clips; active analysis must be cancelled first. Interrupted running analyses are marked failed after a restart; pending analyses remain queued. Run only one backend process at a time.

### Additional tests

Backend tests cover local job states, clip scoring, selection persistence, no-audio handling and chunk boundaries. The optional real-Whisper integration test needs FFmpeg/FFprobe plus `CLIP_TEST_MODEL_DIR` pointing to a local converted Base model and `CLIP_TEST_SPEECH_VIDEO` pointing to a short MP4 containing the public JFK speech fixture; without both it is skipped. For example in PowerShell, set those environment variables before running `pytest`. Frontend tests mock API responses and cover starting analysis, progress, sorting, transcript viewing and selections.


### Optional Groq titles and explanations

This is **off by default**. When explicitly checked, the backend sends the transcript text of up to 30 locally selected candidate clips (in batches of five, up to 6,000 characters per clip) and generated candidate IDs to Groq's chat API, using `llama-3.1-8b-instant`. It does not send the video file. Groq receives transcript text, which may contain private information. Groq's account terms, retention policies, limits and any charges apply; a free tier is not guaranteed. Review generated titles and explanations for accuracy. Clip timestamps, hooks and scores remain local, and Groq does not choose additional candidates or speed up transcription. Local audio transcription still runs unless a transcript for the same model is cached.

Set `GROQ_API_KEY` in the environment of the backend process; it is never sent to the frontend, written to the database or committed. Do **not** paste the key into chat or source code. On Windows, stop both servers and run these commands from the repository root, entering the key only into the masked local prompt:

```powershell
$secure = Read-Host 'Groq API key' -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $env:GROQ_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr); $secure.Dispose() }
powershell -ExecutionPolicy Bypass -File .\start-local.ps1
Remove-Item Env:GROQ_API_KEY
```

The child backend inherits the key until that backend window is closed; the last command removes it from the launcher window. Choose the same Whisper model as your existing transcript, leave re-transcribe unchecked, select **Improve titles and explanations with Groq**, and rerun. Successful reruns replace clips and selections. If Groq fails or returns invalid data, the job fails without replacing your previous results. No automatic provider fallback occurs; you can retry with Groq off. Cancellation can wait for an in-flight network request (45-second network inactivity timeouts). A missing key, model access issue, or rate/quota limit is reported without displaying the key or provider response body. Groq integration is covered by mocked tests only; actual Groq service and Windows use are not yet verified.
