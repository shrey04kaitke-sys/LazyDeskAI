import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from apply_helpers import apply_moves  # noqa: E402


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The database lives NEXT TO the workspace: a workspace that contains LazyDesk's own database is refused.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test.sqlite")
    return workspace


def test_incremental_scan_reuses_cache_and_invalidates_same_size_content(isolated_db: Path):
    document = isolated_db / "notes.txt"
    binary = isolated_db / "unknown.bin"
    document.write_text("hello", encoding="utf-8")
    binary.write_bytes(b"abc")
    client = TestClient(main.app)

    first = client.post("/api/scan", json={"path": str(isolated_db)}).json()
    assert first["processing"]["cache_misses"] == 2
    second = client.post("/api/scan", json={"path": str(isolated_db)}).json()
    assert second["processing"]["cache_hits"] == 2

    original_stat = binary.stat()
    binary.write_bytes(b"xyz")
    os.utime(binary, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    changed = client.post("/api/scan", json={"path": str(isolated_db)}).json()
    assert changed["processing"]["reprocessed"] == 1
    assert changed["processing"]["cache_hits"] == 1


def test_deleted_files_are_removed_from_cache_and_processing_recovers(isolated_db: Path):
    document = isolated_db / "notes.txt"
    document.write_text("hello", encoding="utf-8")
    client = TestClient(main.app)
    client.post("/api/scan", json={"path": str(isolated_db)})
    document.unlink()
    result = client.post("/api/scan", json={"path": str(isolated_db)}).json()
    assert result["count"] == 0
    connection = main.db()
    assert connection.execute("SELECT COUNT(*) FROM processing_cache").fetchone()[0] == 0
    connection.close()


def test_duplicate_detection_requires_exact_content(isolated_db: Path):
    (isolated_db / "one.txt").write_text("same", encoding="utf-8")
    (isolated_db / "two.txt").write_text("same", encoding="utf-8")
    (isolated_db / "similar.txt").write_text("same-ish", encoding="utf-8")
    client = TestClient(main.app)
    result = client.post("/api/duplicates", json={"path": str(isolated_db)}).json()
    assert result["count"] == 1
    assert set(Path(item).name for item in result["groups"][0]["files"]) == {"one.txt", "two.txt"}
    assert len(result["groups"][0]["fingerprint"]) == 64


def test_plan_edit_revalidates_and_execution_is_bound_to_plan(isolated_db: Path):
    document = isolated_db / "notes.txt"
    document.write_text("hello", encoding="utf-8")
    client = TestClient(main.app)
    plan = client.post("/api/plans", json={"workspace_root": str(isolated_db), "files": [str(document)]}).json()
    action = plan["actions"][0]
    edited = client.patch(
        f"/api/plans/{plan['id']}/actions/{action['id']}",
        json={"proposed_name": "renamed.txt"},
    )
    assert edited.status_code == 200
    action = next(item for item in edited.json()["actions"] if item["id"] == action["id"])
    assert action["validation"] == "warning"
    approved = client.post(
        f"/api/plans/{plan['id']}/approve",
        json={"action_ids": [action["id"]]},
    )
    assert approved.status_code == 200
    document.write_text("changed", encoding="utf-8")
    failed = apply_moves(client, plan["id"], [action["id"]])
    assert failed.status_code == 409
    assert document.exists()


def test_path_traversal_and_existing_destination_are_blocked(isolated_db: Path):
    document = isolated_db / "notes.txt"
    document.write_text("hello", encoding="utf-8")
    (isolated_db / "documents").mkdir()
    (isolated_db / "documents" / "occupied.txt").write_text("existing", encoding="utf-8")
    client = TestClient(main.app)
    plan = client.post("/api/plans", json={"workspace_root": str(isolated_db), "files": [str(document)]}).json()
    action = plan["actions"][0]
    traversal = client.patch(
        f"/api/plans/{plan['id']}/actions/{action['id']}",
        json={"proposed_name": "../outside.txt"},
    )
    assert traversal.status_code == 200
    assert traversal.json()["actions"][0]["validation"] == "blocked"
    occupied = client.patch(
        f"/api/plans/{plan['id']}/actions/{action['id']}",
        json={"proposed_name": "occupied.txt"},
    )
    assert occupied.status_code == 200
    assert occupied.json()["actions"][0]["validation"] == "blocked"


def test_synthetic_benchmark_reports_cold_and_warm_runs(isolated_db: Path):
    client = TestClient(main.app)
    result = client.post("/api/benchmarks", json={"synthetic_files": 8})
    assert result.status_code == 200
    payload = result.json()
    assert payload["mode"] == "synthetic"
    assert payload["cold"]["files"] == 8
    assert payload["warm"]["files"] == 8
    assert payload["warm"]["cache_hits"] == 8
    assert payload["baseline_duration_ms"] >= 1
    assert payload["optimized_duration_ms"] >= 1


def test_ollama_invalid_response_falls_back_without_claiming_ai(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    binary = isolated_db / "unknown.bin"
    binary.write_bytes(b"content")
    monkeypatch.setenv("OLLAMA_MODEL", "local-model")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"models": [{"name": "local-model"}]}).encode()

    monkeypatch.setattr(main.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())
    client = TestClient(main.app)
    result = client.post("/api/scan", json={"path": str(isolated_db)}).json()
    item = result["items"][0]
    assert item["classification_source"] == "local-rule"
    assert result["processing"]["model_requests"] == 1


def test_ollama_structured_response_is_normalized_and_kept_as_ai(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    binary = isolated_db / "sample.unknown"
    binary.write_bytes(b"content")
    monkeypatch.setenv("OLLAMA_MODEL", "local-model")

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_urlopen(request, **_kwargs):
        if request.full_url.endswith("/api/tags"):
            return FakeResponse({"models": [{"name": "local-model"}]})
        return FakeResponse({"response": json.dumps({
            "category": "Other",
            "proposed_name": "Sample File",
            "reason": "Unknown extension",
        })})

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    client = TestClient(main.app)
    result = client.post("/api/scan", json={"path": str(isolated_db)}).json()
    item = result["items"][0]
    assert item["classification_source"] == "ollama"
    assert item["category"] == "other"
    assert result["processing"]["model_requests"] == 1
