from fastapi.testclient import TestClient

from app.main import app


def test_projects_persist_and_can_be_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIP_DB_PATH", str(tmp_path / "nested" / "test.sqlite3"))
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok", "database": "connected"}
        assert client.get("/api/projects").json() == []
        response = client.post("/api/projects", json={"name": "  First project  "})
        assert response.status_code == 201
        project = response.json()
        assert project["name"] == "First project"
        assert project["created_at"].endswith("Z")
        assert client.post("/api/projects", json={"name": "   "}).status_code == 422
        assert client.post("/api/projects", json={"name": "x" * 121}).status_code == 422
    # A fresh app lifespan reads the same SQLite file.
    with TestClient(app) as client:
        assert client.get("/api/projects").json() == [project]
        assert client.delete(f"/api/projects/{project['id']}").status_code == 204
        assert client.delete(f"/api/projects/{project['id']}").status_code == 404
        assert client.get("/api/projects").json() == []


def test_database_error_returns_safe_503(tmp_path, monkeypatch):
    import sqlite3
    from app.services import projects

    monkeypatch.setenv("CLIP_DB_PATH", str(tmp_path / "test.sqlite3"))
    with TestClient(app) as client:
        def fail():
            raise sqlite3.OperationalError("private database path")

        monkeypatch.setattr(projects, "check_database", fail)
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json() == {"detail": "Local database unavailable"}


def test_removed_youtube_publishing_keeps_import_and_local_data(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / 'test.sqlite3'
    monkeypatch.setenv('CLIP_DB_PATH', str(path))
    # A database from the previous version may have upload history: leave it untouched.
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE youtube_uploads (render_id TEXT, status TEXT)')
        db.execute("INSERT INTO youtube_uploads VALUES ('old-render', 'uploaded')")
    with TestClient(app) as client:
        assert client.get('/api/youtube/status').status_code == 404
        assert client.post('/api/youtube/connect').status_code == 404
        assert client.post('/api/youtube/upload', json={}).status_code == 404
        # Import is a separate feature and its endpoint remains registered.
        assert client.post('/api/videos/youtube/metadata', json={}).status_code == 422
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM youtube_uploads').fetchall() == [('old-render', 'uploaded')]
