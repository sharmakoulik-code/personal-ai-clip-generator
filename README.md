# Personal AI Clip Generator

A private-by-design, locally running workspace. **Phase 1** provides a dark React/TypeScript/Tailwind dashboard, a FastAPI backend and SQLite-backed project history. Create and delete named projects; they persist after restart. There are no accounts or payment features.

The YouTube URL and file inputs are **disabled**: importing video is Phase 2. Background jobs, AI, video processing, captions, preview and downloads are not implemented. No FFmpeg or API key is needed yet. Local execution does not change GitHub repository visibility; keep the repository private if its contents should not be public.

## Windows: download and run

Install [Git for Windows](https://git-scm.com/download/win), Python 3.11+ (with the `py` launcher), and Node.js 22.12+ on the 22.x line, 24.x, or 26+ (includes npm). Open PowerShell:

```powershell
git clone https://github.com/sharmakoulik-code/personal-ai-clip-generator.git
cd personal-ai-clip-generator
```

Run the startup script from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\start-local.ps1
```

It installs dependencies, then opens separate PowerShell windows for the backend and frontend. Once both are ready, visit **http://127.0.0.1:5173**. To stop, press Ctrl+C in each server window (answer `Y` if asked), then close those windows. If either server fails, its window shows the error. The script checks that ports 8000 and 5173 are available before launching.

### Manual startup

Use two PowerShell terminals, each starting in the repository root.

**Terminal 1 — FastAPI**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**Terminal 2 — React/Vite**

```powershell
cd frontend
npm ci
npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

For later starts, the venv creation and dependency installation steps can be skipped unless dependencies change. The frontend proxies `/api` requests to FastAPI on port 8000. Check the backend at http://127.0.0.1:8000/api/health. Both commands bind to `127.0.0.1` only; **there is no authentication**, so do not expose either server to a network.

The backend creates `backend/data/clips.sqlite3` automatically. The file is ignored by Git: back it up to retain your project history. To change its location, set `$env:CLIP_DB_PATH = 'C:\clips\projects.sqlite3'` in the backend terminal before starting it.

## Tests

From the repository root on Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
cd backend
..\.venv\Scripts\python.exe -m pytest -q
cd ..\frontend
npm test
npm run build
```

Backend tests use a temporary SQLite database. Frontend tests use mocked HTTP responses; the Vite development proxy connects the running frontend to the real backend.
