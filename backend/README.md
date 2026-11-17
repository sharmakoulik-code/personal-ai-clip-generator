# Backend

This FastAPI application initializes a local SQLite database and exposes health and project-library endpoints under `/api`. From the repository root on Windows:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Visit http://127.0.0.1:8000/api/health to check it. The API has no authentication: do not expose it on a network. SQLite defaults to `backend/data/clips.sqlite3`; `CLIP_DB_PATH` overrides its location. Uploading, downloading, AI processing, and rendering are not implemented in Phase 1.
