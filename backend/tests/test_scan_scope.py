from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


def populate(root: Path, count: int) -> None:
    for index in range(count):
        (root / f"file-{index:03d}.txt").write_text(str(index), encoding="utf-8")


# ---- Defect 1: scan completeness -------------------------------------------------

def test_scan_below_cap_reports_complete(tmp_path: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main, "MAX_SCAN_ENTRIES", 5)
    root = tmp_path / "ws"
    root.mkdir()
    populate(root, 3)
    body = client.post("/api/scan", json={"path": str(root)}).json()
    assert body["count"] == 3
    assert body["completeness"] == {
        "scan_root": str(root.resolve()), "files_scanned": 3, "limit": 5, "truncated": False, "complete": True,
    }


def test_scan_exactly_at_cap_is_still_complete(tmp_path: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main, "MAX_SCAN_ENTRIES", 5)
    root = tmp_path / "ws"
    root.mkdir()
    populate(root, 5)
    body = client.post("/api/scan", json={"path": str(root)}).json()
    assert body["count"] == 5
    assert body["completeness"]["truncated"] is False


def test_scan_over_cap_is_flagged_truncated(tmp_path: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main, "MAX_SCAN_ENTRIES", 5)
    root = tmp_path / "ws"
    root.mkdir()
    populate(root, 8)
    body = client.post("/api/scan", json={"path": str(root)}).json()
    assert body["count"] == 5
    completeness = body["completeness"]
    assert completeness["truncated"] is True and completeness["complete"] is False
    assert completeness["files_scanned"] == 5 and completeness["limit"] == 5
    assert completeness["scan_root"] == str(root.resolve())
    activity = client.get("/api/activity").json()["items"][0]["message"]
    assert "INCOMPLETE" in activity


def test_duplicate_scan_reports_truncation(tmp_path: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main, "MAX_SCAN_ENTRIES", 2)
    root = tmp_path / "ws"
    root.mkdir()
    populate(root, 4)
    body = client.post("/api/duplicates", json={"path": str(root)}).json()
    assert body["completeness"]["truncated"] is True


def test_default_cap_is_unchanged():
    assert main.MAX_SCAN_ENTRIES == 10_000


# ---- Defect 2: workspace root is the selected root --------------------------------

def make_nested(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "home"
    nested = root / "academics" / "sem5"
    nested.mkdir(parents=True)
    paper = nested / "paper.pdf"
    paper.write_bytes(b"%PDF")
    return root, paper


def test_workspace_root_is_selected_root_not_parent_of_files(tmp_path: Path, client: TestClient):
    root, paper = make_nested(tmp_path)
    plan = client.post("/api/plans", json={"workspace_root": str(root), "files": [str(paper)]}).json()
    action = plan["actions"][0]
    assert action["workspace_root"] == str(root.resolve())
    assert action["workspace_root"] != str(paper.parent)
    destination = Path(action["proposed_destination"])
    assert destination == paper.parent.resolve() / "documents" / "paper.pdf"
    assert destination.is_relative_to(root.resolve())


def test_nested_files_in_different_folders_share_the_selected_root(tmp_path: Path, client: TestClient):
    root, paper = make_nested(tmp_path)
    other = root / "photos"
    other.mkdir()
    picture = other / "shot.png"
    picture.write_bytes(b"png")
    plan = client.post("/api/plans", json={"workspace_root": str(root), "files": [str(paper), str(picture)]})
    assert plan.status_code == 200
    assert {item["workspace_root"] for item in plan.json()["actions"]} == {str(root.resolve())}


def test_files_outside_selected_root_are_rejected(tmp_path: Path, client: TestClient):
    root, paper = make_nested(tmp_path)
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    response = client.post("/api/plans", json={"workspace_root": str(root), "files": [str(paper), str(outside)]})
    assert response.status_code == 400
    # a sibling directory sharing the root's name prefix must not count as inside it
    sibling = tmp_path / "home-extra"
    sibling.mkdir()
    lookalike = sibling / "a.txt"
    lookalike.write_text("x", encoding="utf-8")
    assert client.post("/api/plans", json={"workspace_root": str(root), "files": [str(lookalike)]}).status_code == 400


def test_traversal_in_file_path_cannot_escape_root(tmp_path: Path, client: TestClient):
    root, _ = make_nested(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("x", encoding="utf-8")
    sneaky = str(root / "academics" / ".." / ".." / "secret.txt")
    assert client.post("/api/plans", json={"workspace_root": str(root), "files": [sneaky]}).status_code == 400


def test_symlinked_directory_escape_is_rejected(tmp_path: Path, client: TestClient):
    root, _ = make_nested(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("x", encoding="utf-8")
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this system")
    response = client.post("/api/plans", json={"workspace_root": str(root), "files": [str(link / "private.txt")]})
    assert response.status_code == 400
    assert (outside / "private.txt").exists()


def test_filesystem_root_and_missing_root_are_refused(tmp_path: Path, client: TestClient):
    anchor = Path(tmp_path.anchor)
    assert client.post("/api/plans", json={"workspace_root": str(anchor), "files": [str(tmp_path / "x.txt")]}).status_code == 400
    assert client.post("/api/plans", json={"workspace_root": str(tmp_path / "missing"), "files": ["a"]}).status_code == 404
    assert client.post("/api/plans", json={"files": ["a"]}).status_code == 422


def test_edit_cannot_move_destination_outside_root_and_execute_stays_inside(tmp_path: Path, client: TestClient):
    root, paper = make_nested(tmp_path)
    plan = client.post("/api/plans", json={"workspace_root": str(root), "files": [str(paper)]}).json()
    action_id = plan["actions"][0]["id"]
    edited = client.patch(f"/api/plans/{plan['id']}/actions/{action_id}", json={"proposed_name": "..\\..\\..\\escape.pdf"})
    assert edited.json()["actions"][0]["validation"] == "blocked"
    client.patch(f"/api/plans/{plan['id']}/actions/{action_id}", json={"proposed_name": "renamed.pdf"})
    assert client.post(f"/api/plans/{plan['id']}/approve", json={"action_ids": [action_id]}).status_code == 200
    result = client.post("/api/execute", json={"plan_id": plan["id"], "action_ids": [action_id]})
    assert result.status_code == 200
    assert (paper.parent / "documents" / "renamed.pdf").exists()
    assert not paper.exists()
    assert not (tmp_path / "escape.pdf").exists()


def test_same_file_selected_twice_yields_one_proposal(tmp_path: Path, client: TestClient):
    root = tmp_path / "ws"
    root.mkdir()
    one = root / "same.txt"
    one.write_text("1", encoding="utf-8")
    plan = client.post("/api/plans", json={"workspace_root": str(root), "files": [str(one), str(one)]}).json()
    assert len(plan["actions"]) == 1
