"""Phase 4A/D: exact Apply preview, single-use confirmation bound to the plan revision, and revision-bound approval.
Synthetic workspaces and databases only."""
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


@pytest.fixture
def workspace(fake_profile) -> Path:
    root = fake_profile.profile / "Documents" / "ws"
    root.mkdir()
    return root


def make_files(root: Path, **files: str) -> list[Path]:
    paths = []
    for name, text in files.items():
        path = root / name.replace("_dot_", ".")
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return paths


def plan_for(client, root: Path, paths: list[Path]) -> dict:
    response = client.post("/api/plans", json={"workspace_root": str(root), "files": [str(p) for p in paths]})
    assert response.status_code == 200, response.text
    return response.json()


def approve(client, plan_id: str, ids, revisions=None):
    body = {"action_ids": list(ids)}
    if revisions is not None:
        body["revisions"] = revisions
    return client.post(f"/api/plans/{plan_id}/approve", json=body)


def approved_plan(client, root: Path, **files: str):
    paths = make_files(root, **files)
    plan = plan_for(client, root, paths)
    ids = [a["id"] for a in plan["actions"]]
    assert approve(client, plan["id"], ids).status_code == 200
    return plan["id"], ids, paths


def preview(client, plan_id, ids):
    return client.post(f"/api/plans/{plan_id}/preview", json={"action_ids": list(ids)})


def execute(client, plan_id, ids, token):
    return client.post("/api/execute", json={"plan_id": plan_id, "action_ids": list(ids), "confirmation_token": token})


def conflict(response, *phrases):
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    text = detail if isinstance(detail, str) else detail["message"]
    for phrase in phrases:
        assert phrase in text, text
    return text


# ---------------------------------------------------------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------------------------------------------------------

def test_preview_lists_the_exact_moves_and_moves_nothing(client, workspace):
    plan_id, ids, paths = approved_plan(client, workspace, a_dot_txt="hello", b_dot_png="png")
    body = preview(client, plan_id, ids).json()
    assert body["count"] == 2 and body["plan_id"] == plan_id and body["expires_in_seconds"] == main.CONFIRMATION_TTL_SECONDS
    by_name = {m["current_name"]: m for m in body["moves"]}
    a = by_name["a.txt"]
    assert a["source"] == str(paths[0]) and a["destination"] == str(workspace / "documents" / "a.txt")
    assert (a["new_name"], a["renamed"], a["category"], a["creates_folder"]) == ("a.txt", False, "documents", True)
    assert a["size"] == 5 and a["fingerprint"] and len(a["fingerprint"]) == 64 and a["name_source"] == "original"
    assert body["creates_folders"] == sorted([str(workspace / "documents"), str(workspace / "images")])
    assert "best-effort" in body["undo_note"] and "not a guarantee" in body["undo_note"]
    assert all(p.exists() for p in paths) and not (workspace / "documents").exists()


def test_preview_shows_a_rename_and_refuses_what_apply_would_refuse(client, workspace):
    paths = make_files(workspace, a_dot_txt="hello")
    plan = plan_for(client, workspace, paths)
    action_id = plan["actions"][0]["id"]
    assert conflict(preview(client, plan["id"], [action_id]), "pending, not approved")
    client.patch(f"/api/plans/{plan['id']}/actions/{action_id}", json={"proposed_name": "renamed.txt"})
    approve(client, plan["id"], [action_id])
    move = preview(client, plan["id"], [action_id]).json()["moves"][0]
    assert (move["current_name"], move["new_name"], move["renamed"], move["name_source"]) == ("a.txt", "renamed.txt", True, "user")
    assert preview(client, plan["id"], []).status_code == 400
    assert preview(client, "nope", ["x"]).status_code == 404
    assert preview(client, plan["id"], ["unknown"]).status_code == 400


def test_preview_runs_the_workspace_policy_again(client, workspace, monkeypatch):
    plan_id, ids, _ = approved_plan(client, workspace, a_dot_txt="x")
    import dataclasses
    from app import workspace_policy as wp
    env = wp.current_environment()
    key = wp._keys(str(workspace.resolve()), env.windows)[0]
    monkeypatch.setattr(wp, "current_environment", lambda: dataclasses.replace(env, inside=env.inside + ((key, "credentials"),)))
    conflict(preview(client, plan_id, ids), "no longer allowed")


# ---------------------------------------------------------------------------------------------------------------------------
# Confirmation token: required, bound, single-use, short-lived
# ---------------------------------------------------------------------------------------------------------------------------

def test_apply_requires_a_confirmation(client, workspace):
    plan_id, ids, paths = approved_plan(client, workspace, a_dot_txt="x")
    conflict(client.post("/api/execute", json={"plan_id": plan_id, "action_ids": ids}), "needs a fresh confirmation")
    conflict(execute(client, plan_id, ids, ""), "needs a fresh confirmation")
    conflict(execute(client, plan_id, ids, "forged-token"), "invalid")
    assert paths[0].exists()


def test_the_happy_path_applies_once_and_the_token_is_single_use(client, workspace):
    plan_id, ids, paths = approved_plan(client, workspace, a_dot_txt="x")
    token = preview(client, plan_id, ids).json()["confirmation_token"]
    done = execute(client, plan_id, ids, token)
    assert done.status_code == 200 and done.json()["status"] == "completed" and done.json()["run_id"] and done.json()["moved"] == ids
    assert (workspace / "documents" / "a.txt").read_text() == "x" and not paths[0].exists()
    conflict(execute(client, plan_id, ids, token), "invalid, already used")


def test_an_expired_confirmation_is_refused_and_moves_nothing(client, workspace, monkeypatch):
    plan_id, ids, paths = approved_plan(client, workspace, a_dot_txt="x")
    now = [1000.0]
    monkeypatch.setattr(main, "CLOCK", lambda: now[0])
    token = preview(client, plan_id, ids).json()["confirmation_token"]
    now[0] += main.CONFIRMATION_TTL_SECONDS - 1
    fresh = preview(client, plan_id, ids).json()["confirmation_token"]
    now[0] += 2                                    # the first token is now past its expiry; the second is still valid
    conflict(execute(client, plan_id, ids, token), "expired")
    assert paths[0].exists() and not (workspace / "documents").exists()
    assert execute(client, plan_id, ids, fresh).status_code == 200


def test_the_token_is_bound_to_the_exact_plan_and_action_set(client, workspace):
    plan_id, ids, paths = approved_plan(client, workspace, a_dot_txt="x", b_dot_txt="y")
    other_id, other_ids, other_paths = approved_plan(client, workspace, c_dot_txt="z")
    one = preview(client, plan_id, ids[:1]).json()["confirmation_token"]
    conflict(execute(client, plan_id, ids, one), "different plan or set of actions")                 # a superset
    two = preview(client, plan_id, ids).json()["confirmation_token"]
    conflict(execute(client, plan_id, ids[:1], two), "different plan or set of actions")             # a subset
    three = preview(client, plan_id, ids).json()["confirmation_token"]
    conflict(execute(client, plan_id, list(reversed(ids)), three), "different plan or set of actions")  # the order is part of the binding
    four = preview(client, other_id, other_ids).json()["confirmation_token"]
    conflict(execute(client, plan_id, ids, four), "different plan or set of actions")                # another plan's token
    assert all(p.exists() for p in paths + other_paths)


def test_client_supplied_revisions_categories_and_fingerprints_are_not_authoritative(client, workspace):
    plan_id, ids, paths = approved_plan(client, workspace, a_dot_txt="x")
    token = preview(client, plan_id, ids).json()["confirmation_token"]
    body = {"plan_id": plan_id, "action_ids": ids, "confirmation_token": token, "revision": 999, "revisions": {ids[0]: 999},
            "category": "images", "source_fingerprint": "0" * 64, "plan_revision": 0, "moves": [{"destination": str(workspace / "evil.txt")}]}
    done = client.post("/api/execute", json=body)
    assert done.status_code == 200
    assert (workspace / "documents" / "a.txt").exists() and not (workspace / "evil.txt").exists() and not (workspace / "images").exists()


CHANGES = {}


def change(name):
    def register(function):
        CHANGES[name] = function
        return function
    return register


@change("action edited")
def _edit(client, plan_id, ids, paths, workspace, monkeypatch):
    assert client.patch(f"/api/plans/{plan_id}/actions/{ids[0]}", json={"proposed_name": "other.txt"}).status_code == 200


@change("another action in the plan rejected")
def _reject_other(client, plan_id, ids, paths, workspace, monkeypatch):
    assert client.post(f"/api/plans/{plan_id}/reject", json={"action_ids": [ids[1]]}).status_code == 200


@change("action approved again after an edit and re-approval")
def _reapprove(client, plan_id, ids, paths, workspace, monkeypatch):
    client.patch(f"/api/plans/{plan_id}/actions/{ids[0]}", json={"proposed_name": "same-ish.txt"})
    client.patch(f"/api/plans/{plan_id}/actions/{ids[0]}", json={"proposed_name": paths[0].name})
    assert approve(client, plan_id, [ids[0]]).status_code == 200


@change("source content changed")
def _content(client, plan_id, ids, paths, workspace, monkeypatch):
    paths[0].write_text("CHANGED", encoding="utf-8")


@change("source deleted")
def _delete(client, plan_id, ids, paths, workspace, monkeypatch):
    paths[0].unlink()


@change("destination occupied")
def _occupied(client, plan_id, ids, paths, workspace, monkeypatch):
    (workspace / "documents").mkdir()
    (workspace / "documents" / paths[0].name).write_text("squatter", encoding="utf-8")


@change("destination folder appeared")
def _folder(client, plan_id, ids, paths, workspace, monkeypatch):
    (workspace / "documents").mkdir()


@change("stored plan edited in the database")
def _tamper(client, plan_id, ids, paths, workspace, monkeypatch):
    connection = main.db()
    items = json.loads(connection.execute("SELECT actions FROM plans WHERE id = ?", (plan_id,)).fetchone()["actions"])
    items[0]["reason"] = "tampered"
    items[0]["source_size"] += 0
    items[0]["category"] = "documents"
    items[0]["name_source"] = "user"
    connection.execute("UPDATE plans SET actions = ? WHERE id = ?", (json.dumps(items), plan_id))
    connection.commit(); connection.close()


@change("workspace became protected")
def _protect(client, plan_id, ids, paths, workspace, monkeypatch):
    import dataclasses
    from app import workspace_policy as wp
    env = wp.current_environment()
    key = wp._keys(str(workspace.resolve()), env.windows)[0]
    monkeypatch.setattr(wp, "current_environment", lambda: dataclasses.replace(env, inside=env.inside + ((key, "credentials"),)))


@pytest.mark.parametrize("name", sorted(CHANGES))
def test_any_change_after_the_preview_invalidates_the_confirmation_and_moves_nothing(client, workspace, monkeypatch, name):
    plan_id, ids, paths = approved_plan(client, workspace, a_dot_txt="aaa", b_dot_txt="bbb")
    token = preview(client, plan_id, ids).json()["confirmation_token"]
    CHANGES[name](client, plan_id, ids, paths, workspace, monkeypatch)
    response = execute(client, plan_id, ids, token)
    assert response.status_code == 409, (name, response.text)
    assert not (workspace / "documents" / "b.txt").exists()
    assert not (workspace / "documents" / "a.txt").exists() or name == "destination occupied"
    assert paths[1].exists()
    connection = main.db()
    assert connection.execute("SELECT COUNT(*) FROM apply_runs").fetchone()[0] == 0
    connection.close()


def test_the_conflict_explains_which_action_changed(client, workspace):
    plan_id, ids, _ = approved_plan(client, workspace, a_dot_txt="a", b_dot_txt="b")
    token = preview(client, plan_id, ids).json()["confirmation_token"]
    client.patch(f"/api/plans/{plan_id}/actions/{ids[1]}", json={"proposed_name": "bb.txt"})
    text = conflict(execute(client, plan_id, ids, token), "Nothing was moved", "changed since the preview", "confirm again")
    assert ids[1] in text and ids[0] not in text.split("changed since")[0]


def test_a_fresh_preview_after_a_change_works(client, workspace):
    plan_id, ids, paths = approved_plan(client, workspace, a_dot_txt="a")
    stale = preview(client, plan_id, ids).json()["confirmation_token"]
    client.patch(f"/api/plans/{plan_id}/actions/{ids[0]}", json={"proposed_name": "new.txt"})
    approve(client, plan_id, ids)
    conflict(execute(client, plan_id, ids, stale), "since the preview")
    fresh = preview(client, plan_id, ids).json()["confirmation_token"]
    assert execute(client, plan_id, ids, fresh).status_code == 200
    assert (workspace / "documents" / "new.txt").exists()


def test_only_a_hash_of_the_token_is_kept_in_memory(client, workspace):
    plan_id, ids, _ = approved_plan(client, workspace, a_dot_txt="a")
    token = preview(client, plan_id, ids).json()["confirmation_token"]
    assert token not in main._CONFIRMATIONS and all(token not in json.dumps(v) for v in main._CONFIRMATIONS.values())


def test_pending_confirmations_are_bounded(client, workspace, monkeypatch):
    monkeypatch.setattr(main, "MAX_PENDING_CONFIRMATIONS", 3)
    plan_id, ids, _ = approved_plan(client, workspace, a_dot_txt="a")
    for _ in range(8):
        preview(client, plan_id, ids)
    assert len(main._CONFIRMATIONS) <= 3


def test_confirmation_and_preview_routes_need_the_api_token(raw_client_factory, workspace):
    raw = raw_client_factory(base_url="http://127.0.0.1:8000")
    assert raw.post("/api/plans/x/preview", json={"action_ids": ["a"]}).status_code == 401
    assert raw.post("/api/execute", json={"plan_id": "x", "action_ids": ["a"], "confirmation_token": "t"}).status_code == 401
    assert raw.get("/api/runs").status_code == 401 and raw.post("/api/runs/x/undo").status_code == 401


# ---------------------------------------------------------------------------------------------------------------------------
# Revision-bound approval (F4)
# ---------------------------------------------------------------------------------------------------------------------------

def test_every_change_to_an_action_bumps_its_revision(client, workspace):
    plan = plan_for(client, workspace, make_files(workspace, a_dot_txt="a"))
    action_id = plan["actions"][0]["id"]
    seen = [plan["actions"][0]["revision"]]
    def revision(response):
        seen.append(next(a for a in response.json()["actions"] if a["id"] == action_id)["revision"])
    revision(client.patch(f"/api/plans/{plan['id']}/actions/{action_id}", json={"proposed_name": "b.txt"}))
    revision(approve(client, plan["id"], [action_id]))
    revision(approve(client, plan["id"], [action_id]))             # already approved: no change, no bump
    revision(client.patch(f"/api/plans/{plan['id']}/actions/{action_id}", json={"proposed_name": "c.txt"}))
    revision(client.post(f"/api/plans/{plan['id']}/reject", json={"action_ids": [action_id]}))
    assert seen == [0, 1, 2, 2, 3, 4]


def test_a_stale_approval_cannot_approve_a_newer_edit(client, workspace):
    paths = make_files(workspace, a_dot_txt="a")
    plan = plan_for(client, workspace, paths)
    action = plan["actions"][0]
    reviewed = {action["id"]: action["revision"]}                  # what the reviewer's screen showed
    client.patch(f"/api/plans/{plan['id']}/actions/{action['id']}", json={"proposed_name": "sneaky.txt"})   # edited elsewhere
    stale = approve(client, plan["id"], [action["id"]], reviewed)
    conflict(stale, "changed since you reviewed it")
    connection = main.db()
    status = json.loads(connection.execute("SELECT actions FROM plans WHERE id = ?", (plan["id"],)).fetchone()["actions"])[0]["status"]
    connection.close()
    assert status == "pending"


def test_approval_with_the_reviewed_revision_succeeds_and_a_missing_entry_is_refused(client, workspace):
    plan = plan_for(client, workspace, make_files(workspace, a_dot_txt="a", b_dot_txt="b"))
    first, second = plan["actions"]
    conflict(approve(client, plan["id"], [first["id"], second["id"]], {first["id"]: first["revision"]}), "changed since you reviewed")
    ok = approve(client, plan["id"], [first["id"], second["id"]], {first["id"]: first["revision"], second["id"]: second["revision"]})
    assert ok.status_code == 200 and [a["status"] for a in ok.json()["actions"]] == ["approved", "approved"]


def test_a_rejected_stale_approval_changes_nothing(client, workspace):
    plan = plan_for(client, workspace, make_files(workspace, a_dot_txt="a", b_dot_txt="b"))
    first, second = plan["actions"]
    client.patch(f"/api/plans/{plan['id']}/actions/{second['id']}", json={"proposed_name": "bb.txt"})
    response = approve(client, plan["id"], [first["id"], second["id"]], {first["id"]: first["revision"], second["id"]: second["revision"]})
    assert response.status_code == 409
    states = [a["status"] for a in client.patch(f"/api/plans/{plan['id']}/actions/{first['id']}", json={"proposed_name": first["proposed_name"]}).json()["actions"]]
    assert states == ["pending", "pending"]
