from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from apply_helpers import apply_moves  # noqa: E402


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


def make_plan(client: TestClient, *files: Path, root: Path | None = None) -> dict:
    response = client.post("/api/plans", json={"workspace_root": str(root or files[0].parent), "files": [str(item) for item in files]})
    assert response.status_code == 200, response.text
    return response.json()


def approve(client: TestClient, plan: dict, *ids: str):
    return client.post(f"/api/plans/{plan['id']}/approve", json={"action_ids": list(ids)})


def execute(client: TestClient, plan: dict, *ids: str):
    return apply_moves(client, plan["id"], list(ids))


def test_approved_action_moves_file_and_cannot_run_twice(workspace: Path, client: TestClient):
    source = workspace / "notes.txt"
    source.write_text("hello", encoding="utf-8")
    plan = make_plan(client, source)
    action_id = plan["actions"][0]["id"]
    assert approve(client, plan, action_id).status_code == 200
    result = execute(client, plan, action_id)
    assert result.status_code == 200
    assert result.json()["moved"] == [action_id]
    assert not source.exists()
    assert (workspace / "documents" / "notes.txt").read_text(encoding="utf-8") == "hello"
    replay = execute(client, plan, action_id)
    assert replay.status_code == 409


def test_unapproved_action_never_executes(workspace: Path, client: TestClient):
    source = workspace / "notes.txt"
    source.write_text("hello", encoding="utf-8")
    plan = make_plan(client, source)
    response = execute(client, plan, plan["actions"][0]["id"])
    assert response.status_code == 409
    assert source.exists()


def test_rejected_action_never_executes_and_cannot_be_approved(workspace: Path, client: TestClient):
    source = workspace / "notes.txt"
    source.write_text("hello", encoding="utf-8")
    plan = make_plan(client, source)
    action_id = plan["actions"][0]["id"]
    assert plan["actions"][0]["validation"] in ("safe", "warning")
    client.post(f"/api/plans/{plan['id']}/reject", json={"action_ids": [action_id]})
    assert approve(client, plan, action_id).status_code == 409
    assert execute(client, plan, action_id).status_code == 409
    assert source.exists()


def test_rejection_after_approval_blocks_execution(workspace: Path, client: TestClient):
    source = workspace / "notes.txt"
    source.write_text("hello", encoding="utf-8")
    plan = make_plan(client, source)
    action_id = plan["actions"][0]["id"]
    approve(client, plan, action_id)
    client.post(f"/api/plans/{plan['id']}/reject", json={"action_ids": [action_id]})
    assert execute(client, plan, action_id).status_code == 409
    assert source.exists()


def test_edit_invalidates_prior_approval_and_keeps_rejection(workspace: Path, client: TestClient):
    first, second = workspace / "a.txt", workspace / "b.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    plan = make_plan(client, first, second)
    a_id, b_id = (item["id"] for item in sorted(plan["actions"], key=lambda item: item["source"]))
    approve(client, plan, a_id)
    client.post(f"/api/plans/{plan['id']}/reject", json={"action_ids": [b_id]})
    edited_a = client.patch(f"/api/plans/{plan['id']}/actions/{a_id}", json={"proposed_name": "renamed.txt"}).json()
    edited_b = client.patch(f"/api/plans/{plan['id']}/actions/{b_id}", json={"proposed_name": "other.txt"}).json()
    statuses = {item["id"]: item["status"] for item in edited_b["actions"]}
    assert statuses[a_id] == "pending"
    assert statuses[b_id] == "rejected"
    assert execute(client, plan, a_id).status_code == 409
    assert execute(client, plan, b_id).status_code == 409
    assert first.exists() and second.exists()
    assert edited_a["actions"]


def test_actions_from_another_plan_are_refused(workspace: Path, client: TestClient):
    first, second = workspace / "a.txt", workspace / "b.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    plan_one = make_plan(client, first)
    plan_two = make_plan(client, second)
    foreign = plan_two["actions"][0]["id"]
    approve(client, plan_two, foreign)
    assert approve(client, plan_one, foreign).status_code == 400
    assert execute(client, plan_one, foreign).status_code == 400
    assert first.exists() and second.exists()


def test_changed_source_blocks_approval_and_execution(workspace: Path, client: TestClient):
    source = workspace / "notes.txt"
    source.write_text("hello", encoding="utf-8")
    plan = make_plan(client, source)
    action_id = plan["actions"][0]["id"]
    approve(client, plan, action_id)
    source.write_text("changed after approval", encoding="utf-8")
    assert execute(client, plan, action_id).status_code == 409
    assert source.read_text(encoding="utf-8") == "changed after approval"
    assert approve(client, plan, action_id).status_code == 409


def test_destination_created_after_approval_is_not_overwritten(workspace: Path, client: TestClient):
    source = workspace / "notes.txt"
    source.write_text("hello", encoding="utf-8")
    plan = make_plan(client, source)
    action_id = plan["actions"][0]["id"]
    approve(client, plan, action_id)
    (workspace / "documents").mkdir()
    (workspace / "documents" / "notes.txt").write_text("existing", encoding="utf-8")
    assert execute(client, plan, action_id).status_code == 409
    assert (workspace / "documents" / "notes.txt").read_text(encoding="utf-8") == "existing"
    assert source.exists()


def test_missing_source_and_unknown_plan_are_refused(workspace: Path, client: TestClient):
    source = workspace / "notes.txt"
    source.write_text("hello", encoding="utf-8")
    plan = make_plan(client, source)
    action_id = plan["actions"][0]["id"]
    approve(client, plan, action_id)
    source.unlink()
    assert execute(client, plan, action_id).status_code == 409
    assert client.post("/api/execute", json={"plan_id": "nope", "action_ids": ["x"]}).status_code == 404
    assert client.post("/api/execute", json={"plan_id": plan["id"], "action_ids": []}).status_code == 400


def test_invalid_names_are_blocked_and_cannot_be_approved(workspace: Path, client: TestClient):
    source = workspace / "notes.txt"
    source.write_text("hello", encoding="utf-8")
    plan = make_plan(client, source)
    action_id = plan["actions"][0]["id"]
    for bad in ["../escape.txt", "sub/dir.txt", "back\\slash.txt", "..", "bad\x01name.txt"]:
        edited = client.patch(f"/api/plans/{plan['id']}/actions/{action_id}", json={"proposed_name": bad})
        assert edited.status_code == 200
        assert edited.json()["actions"][0]["validation"] == "blocked", bad
        assert approve(client, plan, action_id).status_code == 409, bad
        assert execute(client, plan, action_id).status_code == 409, bad
    assert source.exists()
    assert not (workspace.parent / "escape.txt").exists()


def test_tampered_stored_destination_outside_workspace_is_refused(workspace: Path, client: TestClient):
    source = workspace / "notes.txt"
    source.write_text("hello", encoding="utf-8")
    plan = make_plan(client, source)
    action_id = plan["actions"][0]["id"]
    approve(client, plan, action_id)
    connection = main.db()
    _, actions = main.load_plan(connection, plan["id"])
    actions[0].proposed_destination = str(workspace.parent / "escaped.txt")
    main.save_plan(connection, plan["id"], actions)
    connection.close()
    assert execute(client, plan, action_id).status_code == 409
    assert source.exists() and not (workspace.parent / "escaped.txt").exists()


def test_batch_with_one_bad_action_changes_nothing(workspace: Path, client: TestClient):
    good, stale = workspace / "a.txt", workspace / "b.txt"
    good.write_text("a", encoding="utf-8")
    stale.write_text("b", encoding="utf-8")
    plan = make_plan(client, good, stale)
    ids = [item["id"] for item in plan["actions"]]
    approve(client, plan, *ids)
    stale.write_text("bb", encoding="utf-8")
    assert execute(client, plan, *ids).status_code == 409
    assert good.exists() and stale.exists()
    assert not (workspace / "documents").exists()


def test_partial_failure_reports_moved_actions_and_is_not_rolled_back(workspace: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch):
    first, second, third = workspace / "a.txt", workspace / "b.txt", workspace / "c.txt"
    for item in (first, second, third):
        item.write_text(item.name, encoding="utf-8")
    plan = make_plan(client, first, second, third)
    by_source = {Path(item["source"]).name: item["id"] for item in plan["actions"]}
    ordered = [by_source["a.txt"], by_source["b.txt"], by_source["c.txt"]]
    approve(client, plan, *ordered)
    real_move = main.move_file
    calls = {"count": 0}

    def flaky_move(source: Path, destination: Path) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated disk failure")
        real_move(source, destination)

    monkeypatch.setattr(main, "move_file", flaky_move)
    response = execute(client, plan, *ordered)
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["moved"] == [by_source["a.txt"]]
    assert detail["not_attempted"] == [by_source["c.txt"]]
    assert (workspace / "documents" / "a.txt").exists()   # earlier move stays applied (no rollback)
    assert second.exists() and third.exists()
    monkeypatch.setattr(main, "move_file", real_move)
    retry = execute(client, plan, *ordered)
    assert retry.status_code == 409                        # already-executed action cannot be replayed
    assert execute(client, plan, by_source["b.txt"], by_source["c.txt"]).status_code == 200


def test_symlinked_source_is_not_accepted(workspace: Path, client: TestClient, tmp_path: Path):
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this system")
    response = client.post("/api/plans", json={"workspace_root": str(workspace), "files": [str(link)]})
    assert response.status_code == 400
    assert target.exists()
