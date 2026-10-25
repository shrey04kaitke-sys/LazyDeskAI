import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from apply_helpers import apply_moves  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


def scan(client: TestClient, root: Path) -> dict:
    return client.post("/api/scan", json={"path": str(root)}).json()


def swap_content_keeping_size_and_mtime(path: Path, content: bytes) -> None:
    before = path.stat()
    assert len(content) == before.st_size
    path.write_bytes(content)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


# ---- cache correctness ---------------------------------------------------------------

def test_known_extension_result_depends_only_on_the_extension_so_content_swaps_cannot_make_it_stale(workspace, client):
    note = workspace / "notes.txt"
    note.write_bytes(b"aaaa")
    assert scan(client, workspace)["items"][0]["category"] == "documents"
    swap_content_keeping_size_and_mtime(note, b"bbbb")
    again = scan(client, workspace)
    assert again["processing"]["cache_hits"] == 1  # reuse is correct: the classification cannot differ
    assert again["items"][0]["category"] == "documents"


def test_plan_fingerprint_catches_a_same_size_same_mtime_swap_of_a_known_extension_file(workspace, client):
    note = workspace / "notes.txt"
    note.write_bytes(b"aaaa")
    created = client.post("/api/plans", json={"workspace_root": str(workspace), "files": [str(note)]}).json()
    action = created["actions"][0]
    assert action["source_fingerprint"] and len(action["source_fingerprint"]) == 64
    assert client.post(f"/api/plans/{created['id']}/approve", json={"action_ids": [action["id"]]}).status_code == 200
    swap_content_keeping_size_and_mtime(note, b"bbbb")
    response = apply_moves(client, created["id"], [action["id"]])
    assert response.status_code == 409 and "content changed" in response.json()["detail"]
    assert note.read_bytes() == b"bbbb" and not (workspace / "documents").exists()


def test_approval_also_rechecks_the_content_fingerprint(workspace, client):
    note = workspace / "notes.txt"
    note.write_bytes(b"aaaa")
    created = client.post("/api/plans", json={"workspace_root": str(workspace), "files": [str(note)]}).json()
    swap_content_keeping_size_and_mtime(note, b"bbbb")
    response = client.post(f"/api/plans/{created['id']}/approve", json={"action_ids": [created["actions"][0]["id"]]})
    assert response.status_code == 409


def test_unknown_extension_content_swap_with_same_size_and_mtime_is_reprocessed(workspace, client):
    blob = workspace / "blob.bin"
    blob.write_bytes(b"abc")
    scan(client, workspace)
    swap_content_keeping_size_and_mtime(blob, b"xyz")
    again = scan(client, workspace)
    assert again["processing"]["reprocessed"] == 1 and again["processing"]["cache_hits"] == 0


def test_cloud_only_and_oversized_files_are_never_read_for_fingerprints(workspace, client, monkeypatch):
    note = workspace / "notes.txt"
    blob = workspace / "blob.bin"
    note.write_bytes(b"aaaa")
    blob.write_bytes(b"abc")

    def forbidden(_path):
        raise AssertionError("file content must not be read")

    monkeypatch.setattr(main, "is_cloud_placeholder", lambda _path: True)
    monkeypatch.setattr(main, "content_fingerprint", forbidden)
    first = scan(client, workspace)
    assert first["count"] == 2
    created = client.post("/api/plans", json={"workspace_root": str(workspace), "files": [str(note), str(blob)]}).json()
    assert all(item["source_fingerprint"] is None for item in created["actions"])
    assert scan(client, workspace)["processing"]["cache_hits"] == 2  # still reusable by size + mtime

    monkeypatch.setattr(main, "is_cloud_placeholder", lambda _path: False)
    monkeypatch.setattr(main, "FINGERPRINT_LIMIT_BYTES", 2)
    assert main.identity_fingerprint(note) is None


def test_cloud_attribute_detection(workspace, monkeypatch):
    note = workspace / "notes.txt"
    note.write_bytes(b"x")
    real = Path.stat

    class FakeStat:
        def __init__(self, attributes):
            self.st_file_attributes, self.st_size = attributes, 1

    monkeypatch.setattr(Path, "stat", lambda self, **kw: FakeStat(0x400000) if self == note else real(self, **kw))
    assert main.is_cloud_placeholder(note) is True
    monkeypatch.setattr(Path, "stat", lambda self, **kw: FakeStat(0x20) if self == note else real(self, **kw))
    assert main.is_cloud_placeholder(note) is False


def test_new_processing_version_invalidates_every_cached_row(workspace, client, monkeypatch):
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    (workspace / "b.bin").write_bytes(b"b")
    scan(client, workspace)
    monkeypatch.setattr(main, "PROCESSING_VERSION", "rules-next")
    again = scan(client, workspace)
    assert again["processing"]["cache_hits"] == 0 and again["processing"]["reprocessed"] == 2


def test_interrupted_processing_row_is_not_reused(workspace, client):
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    scan(client, workspace)
    connection = main.db()
    connection.execute("UPDATE processing_cache SET processing_status = 'processing'")
    connection.commit()
    connection.close()
    assert scan(client, workspace)["processing"]["cache_hits"] == 0


def test_database_created_before_model_suggestions_is_migrated_in_place(tmp_path: Path, monkeypatch):
    legacy = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(legacy)
    connection.executescript(
        """
        CREATE TABLE processing_cache (
            workspace_root TEXT NOT NULL, relative_path TEXT NOT NULL, file_size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL, content_fingerprint TEXT, classification TEXT NOT NULL,
            classification_source TEXT NOT NULL, processing_status TEXT NOT NULL,
            cache_schema_version INTEGER NOT NULL, processing_version TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (workspace_root, relative_path));
        INSERT INTO processing_cache VALUES ('r', 'a.txt', 1, 1, NULL, 'documents', 'local-rule', 'complete', 1, 'rules-v1', 't');
        """
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(main, "DB_PATH", legacy)
    migrated = main.db()
    columns = {row["name"] for row in migrated.execute("PRAGMA table_info(processing_cache)")}
    assert "details" in columns
    assert migrated.execute("SELECT COUNT(*) FROM processing_cache").fetchone()[0] == 1  # data preserved
    migrated.close()
    main.db().close()  # opening again is a no-op


# ---- duplicate detection reads only what it must -------------------------------------

def test_duplicate_scan_hashes_only_files_that_share_a_size(workspace, client, monkeypatch):
    (workspace / "one.txt").write_text("same", encoding="utf-8")
    (workspace / "two.txt").write_text("same", encoding="utf-8")
    (workspace / "unique.txt").write_text("this one has a unique size", encoding="utf-8")
    hashed = []
    real = main.content_fingerprint
    monkeypatch.setattr(main, "content_fingerprint", lambda path: hashed.append(path.name) or real(path))
    body = client.post("/api/duplicates", json={"path": str(workspace)}).json()
    assert body["count"] == 1 and sorted(hashed) == ["one.txt", "two.txt"] and body["files_hashed"] == 2


def test_duplicate_scan_does_not_download_cloud_only_files(workspace, client, monkeypatch):
    (workspace / "one.txt").write_text("same", encoding="utf-8")
    (workspace / "two.txt").write_text("same", encoding="utf-8")
    (workspace / "three.txt").write_text("same", encoding="utf-8")
    monkeypatch.setattr(main, "is_cloud_placeholder", lambda path: path.name == "three.txt")
    body = client.post("/api/duplicates", json={"path": str(workspace)}).json()
    assert body["skipped_cloud_only"] == 1
    assert [Path(name).name for name in body["groups"][0]["files"]] == ["one.txt", "two.txt"]


# ---- benchmark -----------------------------------------------------------------------

def test_synthetic_benchmark_counts_are_accurate_and_inputs_equivalent(client):
    payload = client.post("/api/benchmarks", json={"synthetic_files": 20, "repetitions": 3}).json()
    assert payload["mode"] == "synthetic" and payload["files_discovered"] == 20
    assert payload["cold"] == {"files": 20, "processed": 20, "cache_hits": 0, "duration_ms": payload["cold"]["duration_ms"]}
    assert (payload["optimized_cold"]["processed"], payload["optimized_cold"]["cache_hits"]) == (20, 0)
    assert (payload["warm"]["processed"], payload["warm"]["cache_hits"]) == (0, 20)
    assert payload["cache_hits"] == 20 and payload["cache_misses"] == 0 and payload["unchanged_files_skipped"] == 20
    assert payload["results_match"] is True and payload["local_model_requests"] == 0
    assert {name: len(values) for name, values in payload["samples"].items()} == {"no_cache": 3, "cold_cache": 3, "warm_cache": 3}
    assert payload["baseline_duration_ms"] >= 1 and payload["optimized_duration_ms"] >= 1
    assert "not expected to beat" in payload["methodology"]
    assert not Path(payload["path"]).exists()  # synthetic folder cleaned up


def test_benchmark_never_touches_the_application_cache_or_the_files(workspace, client):
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    (workspace / "b.bin").write_bytes(b"b")
    scan(client, workspace)  # populate the application's own cache first
    connection = main.db()
    before = connection.execute("SELECT * FROM processing_cache ORDER BY relative_path").fetchall()
    before = [tuple(row) for row in before]
    connection.close()
    listing = sorted((p.name, p.stat().st_mtime_ns) for p in workspace.iterdir())
    payload = client.post("/api/benchmarks", json={"path": str(workspace)}).json()
    assert payload["optimized_cold"]["cache_hits"] == 0  # truly cold even though the app cache was warm
    assert payload["warm"]["cache_hits"] == 2
    connection = main.db()
    after = [tuple(row) for row in connection.execute("SELECT * FROM processing_cache ORDER BY relative_path").fetchall()]
    history = connection.execute("SELECT COUNT(*) FROM benchmarks").fetchone()[0]
    connection.close()
    assert after == before and history == 1
    assert sorted((p.name, p.stat().st_mtime_ns) for p in workspace.iterdir()) == listing


def test_synthetic_benchmark_leaves_no_cache_rows_in_the_application_database(client):
    client.post("/api/benchmarks", json={"synthetic_files": 10})
    connection = main.db()
    assert connection.execute("SELECT COUNT(*) FROM processing_cache").fetchone()[0] == 0
    connection.close()


def test_benchmark_validates_its_request(client):
    assert client.post("/api/benchmarks", json={}).status_code == 400
    assert client.post("/api/benchmarks", json={"synthetic_files": 5, "repetitions": 0}).status_code == 422
    assert client.post("/api/benchmarks", json={"synthetic_files": 5, "repetitions": 10}).status_code == 422
    assert client.post("/api/benchmarks", json={"synthetic_files": 5000}).status_code == 422


def test_benchmark_reports_truncation_in_its_methodology(workspace, client, monkeypatch):
    monkeypatch.setattr(main, "MAX_SCAN_ENTRIES", 2)
    for index in range(4):
        (workspace / f"f{index}.txt").write_text("x", encoding="utf-8")
    payload = client.post("/api/benchmarks", json={"path": str(workspace), "repetitions": 1}).json()
    assert payload["files_discovered"] == 2 and "limit" in payload["methodology"]


def test_median_helper():
    assert main.median([3.0, 1.0, 2.0]) == 2.0
    assert main.median([1.0, 2.0, 3.0, 10.0]) == 2.5
