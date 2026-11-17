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
