from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_local_status():
    response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_scan_rejects_missing_directory():
    response = client.post("/api/scan", json={"path": str(Path("C:/definitely-missing-lazydesk"))})
    assert response.status_code == 404


def test_scan_and_plan(tmp_path: Path):
    document = tmp_path / "notes.txt"
    document.write_text("hello", encoding="utf-8")
    scan_response = client.post("/api/scan", json={"path": str(tmp_path)})
    assert scan_response.status_code == 200
    assert scan_response.json()["count"] == 1

    plan_response = client.post("/api/plans", json={"workspace_root": str(tmp_path), "files": [str(document)]})
    assert plan_response.status_code == 200
    assert plan_response.json()["actions"][0]["validation"] == "warning"
