# Personal AI Clip Generator

A private, local workspace for turning videos into short clips. **Phase 1 only** is implemented: a React/TypeScript/Tailwind dashboard, FastAPI API, and SQLite-backed project library. You can create, list, and delete named projects; the dashboard reports backend connectivity and persists history across restarts.

Video uploads and YouTube importing, background processing, transcription, AI, rendering, captions, editing and downloads are **not implemented yet**. The dashboard's roadmap describes future work, not working features. No account, cloud service, or API key is required for Phase 1.

## Run locally on Windows

Prerequisites: Python 3.11 or newer, Node.js 22.12+ on the 22.x line, 24.x, or 26+ (Node 22 LTS recommended), and npm. Open PowerShell in the repository directory. Run the backend and frontend in separate terminals:

**Terminal 1 — backend**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**Terminal 2 — frontend**

```powershell
cd frontend
npm ci
npm run dev
```

Open the localhost URL printed by Vite (normally http://localhost:5173). Keep both terminals open. The frontend proxies `/api` requests to the local backend; start the backend on port 8000. Stop either server with Ctrl+C. The SQLite database is created automatically at `backend/data/clips.sqlite3` and is not tracked by Git. To use a different database location, set `CLIP_DB_PATH` in the backend terminal before starting it (e.g. `$env:CLIP_DB_PATH = "C:\clips\projects.sqlite3"`). Back up this file to retain project history.

The servers bind to localhost by default and have **no authentication**, so do not expose them to the internet or an untrusted network. No FFmpeg, AI model, or API key is needed yet.

## Tests

Install backend test dependencies and run:

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
cd backend
..\.venv\Scripts\python.exe -m pytest -q
```

In another terminal, run frontend tests and a production build:

```powershell
cd frontend
npm ci
npm test
npm run build
```

The frontend tests use mocked HTTP responses; backend tests use a temporary SQLite database. The Vite development proxy is used for the local running experience.
