"""Phase 4B/C: durable per-file journal, crash recovery, partial failure and best-effort Undo.
All moves happen in synthetic temporary workspaces with temporary databases; crashes are simulated with main.FAULT_HOOK."""
import os
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import journal, main


class SimulatedCrash(BaseException):
    """Not an Exception: nothing in the application may catch it, exactly like a killed process."""


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


def approved(client, root: Path, **files: str):
    paths = make_files(root, **files)
    plan = client.post("/api/plans", json={"workspace_root": str(root), "files": [str(p) for p in paths]}).json()
    ids = [a["id"] for a in plan["actions"]]
    assert client.post(f"/api/plans/{plan['id']}/approve", json={"action_ids": ids}).status_code == 200
    return plan["id"], ids, paths


def apply(client, plan_id, ids):
    preview = client.post(f"/api/plans/{plan_id}/preview", json={"action_ids": list(ids)})
    if preview.status_code != 200:
        return preview
    return client.post("/api/execute", json={"plan_id": plan_id, "action_ids": list(ids), "confirmation_token": preview.json()["confirmation_token"]})


def read(sql: str, *args):
    connection = sqlite3.connect(main.DB_PATH)     # a SEPARATE connection: proves what is durable, not what is pending
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, args).fetchall()
    finally:
        connection.close()


def states(run_id: str) -> list[str]:
    return [row["state"] for row in read("SELECT state FROM apply_moves WHERE run_id = ? ORDER BY seq", run_id)]


def only_run() -> sqlite3.Row:
    rows = read("SELECT * FROM apply_runs")
    assert len(rows) == 1
    return rows[0]


def plan_statuses(plan_id: str) -> list[str]:
    import json
    return [a["status"] for a in json.loads(read("SELECT actions FROM plans WHERE id = ?", plan_id)[0]["actions"])]


def crash_at(monkeypatch, point: str, seq: int, observe=None):
    def hook(where: str, at: int):
        if where == point and at == seq:
            if observe:
                observe()
            raise SimulatedCrash(point)
    monkeypatch.setattr(main, "FAULT_HOOK", hook)


def restart() -> list[dict]:
    """What startup does: settle whatever the dead process left behind."""
    main.FAULT_HOOK = None
    main.ACTIVE_RUNS.clear()
    return main.reconcile_interrupted()


# ---------------------------------------------------------------------------------------------------------------------------
# Journal contents and durability
# ---------------------------------------------------------------------------------------------------------------------------

def test_a_successful_run_is_journaled_move_by_move(client, workspace):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="aaa", b_dot_png="bbb")
    done = apply(client, plan_id, ids).json()
    run = only_run()
    assert run["id"] == done["run_id"] and run["status"] == "completed" and run["finished_at"] and run["plan_id"] == plan_id
    assert run["workspace_root"] == str(workspace.resolve()) and run["plan_revision"] >= 1
    moves = read("SELECT * FROM apply_moves ORDER BY seq")
    assert [m["state"] for m in moves] == ["moved", "moved"] and [m["seq"] for m in moves] == [0, 1]
    assert [m["source"] for m in moves] == [str(p) for p in paths]
    assert moves[0]["destination"] == str(workspace / "documents" / "a.txt") and moves[0]["created_folder"] == str(workspace / "documents")
    assert moves[0]["size"] == 3 and len(moves[0]["fingerprint"]) == 64 and moves[0]["mtime_ns"] > 0
    assert plan_statuses(plan_id) == ["executed", "executed"]


def test_the_planned_record_is_durable_before_the_file_moves(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="aaa")
    seen = {}

    def observe():
        seen["states"] = [r["state"] for r in read("SELECT state FROM apply_moves")]
        seen["run"] = [r["status"] for r in read("SELECT status FROM apply_runs")]
        seen["at_source"] = paths[0].exists()
        seen["at_destination"] = (workspace / "documents" / "a.txt").exists()
    crash_at(monkeypatch, "moving", 0, observe)
    with pytest.raises(SimulatedCrash):
        apply(client, plan_id, ids)
    assert seen == {"states": ["moving"], "run": ["running"], "at_source": True, "at_destination": False}


def test_the_moved_record_is_written_right_after_the_move(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="aaa", b_dot_txt="bbb")
    seen = []
    real = main.move_file

    def spying_move(source, destination):
        seen.append([r["state"] for r in read("SELECT state FROM apply_moves ORDER BY seq")])
        real(source, destination)
    monkeypatch.setattr(main, "move_file", spying_move)
    assert apply(client, plan_id, ids).status_code == 200
    assert seen == [["moving", "planned"], ["moved", "moving"]]


# ---------------------------------------------------------------------------------------------------------------------------
# Partial failure
# ---------------------------------------------------------------------------------------------------------------------------

def test_a_failure_stops_safely_and_reports_done_failed_and_unattempted(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="a", b_dot_txt="b", c_dot_txt="c")
    real = main.move_file

    def flaky(source, destination):
        if source.name == "b.txt":
            raise PermissionError("locked by another program")
        real(source, destination)
    monkeypatch.setattr(main, "move_file", flaky)
    response = apply(client, plan_id, ids)
    detail = response.json()["detail"]
    by = {p.name: i for p, i in zip(paths, ids)}
    assert response.status_code == 500 and detail["status"] == "partial" and "locked by another program" in detail["message"]
    assert detail["moved"] == [by["a.txt"]] and detail["failed"] == by["b.txt"] and detail["not_attempted"] == [by["c.txt"]]
    run = only_run()
    assert run["status"] == "partial" and states(run["id"]) == ["moved", "failed", "skipped"]
    assert (workspace / "documents" / "a.txt").exists() and paths[1].exists() and paths[2].exists()
    assert plan_statuses(plan_id) == ["executed", "approved", "approved"]


def test_a_run_whose_first_move_fails_is_failed_not_complete(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="a", b_dot_txt="b")
    monkeypatch.setattr(main, "move_file", lambda s, d: (_ for _ in ()).throw(PermissionError("denied")))
    assert apply(client, plan_id, ids).status_code == 500
    run = only_run()
    assert run["status"] == "failed" and states(run["id"]) == ["failed", "skipped"] and all(p.exists() for p in paths)


def test_run_status_is_derived_from_the_moves_and_complete_needs_every_move():
    d = journal.derive_run_status
    assert d(["moved", "moved"]) == "completed"
    assert d(["moved", "failed"]) == "partial" and d(["moved", "skipped"]) == "partial" and d(["moved", "planned"]) == "partial"
    assert d(["failed", "skipped"]) == "failed" and d(["planned"]) == "failed" and d(["skipped"]) == "failed"
    assert d(["moved", "moving"]) == "running" and d(["moved"], active=True) == "running"
    assert d(["moved", "needs_attention"]) == "needs_attention" and d(["undoing", "moved"]) == "undoing"
    assert d(["undone", "undone"]) == "undone" and d(["undone", "moved"]) == "undo_partial"
    assert d(["undo_blocked", "moved"]) == "undo_blocked" and d(["undone", "undo_blocked"]) == "undo_partial"
    assert d(["moved", "planned", "moved"]) != "completed"


def test_a_journal_write_failure_after_a_move_is_not_reported_as_success(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="a")

    def hook(point, seq):
        if point == "journal":
            raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(main, "FAULT_HOOK", hook)
    response = client.post("/api/plans/%s/preview" % plan_id, json={"action_ids": ids})
    result = client.post("/api/execute", json={"plan_id": plan_id, "action_ids": ids, "confirmation_token": response.json()["confirmation_token"]})
    assert result.status_code == 500 and "journal could not be updated" in result.json()["detail"]["message"]
    assert result.json()["detail"]["moved"] == []
    assert (workspace / "documents" / "a.txt").exists() and not paths[0].exists()
    assert states(only_run()["id"]) == ["moving"] and only_run()["status"] == "running"      # honest: not complete
    assert plan_statuses(plan_id) == ["approved"]
    monkeypatch.setattr(main, "FAULT_HOOK", None)
    report = main.reconcile_interrupted()                                                  # the next start settles it from the file system
    assert [r["to"] for r in report] == ["moved"]
    assert only_run()["status"] == "completed" and plan_statuses(plan_id) == ["executed"]


# ---------------------------------------------------------------------------------------------------------------------------
# Crashes and startup reconciliation
# ---------------------------------------------------------------------------------------------------------------------------

def test_crash_before_any_move_is_settled_as_nothing_happened(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="a", b_dot_txt="b")
    crash_at(monkeypatch, "planned", -1)
    with pytest.raises(SimulatedCrash):
        apply(client, plan_id, ids)
    assert only_run()["status"] == "running" and states(only_run()["id"]) == ["planned", "planned"]
    report = restart()
    assert {r["to"] for r in report} == {"skipped"}
    assert only_run()["status"] == "failed" and all(p.exists() for p in paths) and plan_statuses(plan_id) == ["approved", "approved"]
    assert apply(client, plan_id, ids).status_code == 200                                    # and the plan can simply be applied again


def test_crash_after_the_journal_but_before_the_move_is_settled_as_failed(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="a")
    crash_at(monkeypatch, "moving", 0)
    with pytest.raises(SimulatedCrash):
        apply(client, plan_id, ids)
    assert states(only_run()["id"]) == ["moving"] and paths[0].exists()
    report = restart()
    assert [(r["from"], r["to"]) for r in report] == [("moving", "failed")]
    assert only_run()["status"] == "failed" and paths[0].exists() and plan_statuses(plan_id) == ["approved"]


def test_crash_after_folder_creation_removes_only_the_empty_folder_it_made(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="a")
    crash_at(monkeypatch, "after_mkdir", 0)
    with pytest.raises(SimulatedCrash):
        apply(client, plan_id, ids)
    assert (workspace / "documents").is_dir() and paths[0].exists()
    restart()
    assert not (workspace / "documents").exists() and paths[0].exists()


def test_a_folder_that_is_no_longer_empty_is_never_removed_on_recovery(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="a")
    crash_at(monkeypatch, "after_mkdir", 0)
    with pytest.raises(SimulatedCrash):
        apply(client, plan_id, ids)
    (workspace / "documents" / "user-file.txt").write_text("precious", encoding="utf-8")
    restart()
    assert (workspace / "documents" / "user-file.txt").read_text() == "precious"


def test_crash_after_the_move_but_before_the_journal_update_is_recovered_as_moved(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="a", b_dot_txt="b")
    crash_at(monkeypatch, "moved", 0)
    with pytest.raises(SimulatedCrash):
        apply(client, plan_id, ids)
    assert states(only_run()["id"]) == ["moving", "planned"] and (workspace / "documents" / "a.txt").exists()
    report = restart()
    assert sorted((r["from"], r["to"]) for r in report) == [("moving", "moved"), ("planned", "skipped")]
    run = only_run()
    assert states(run["id"]) == ["moved", "skipped"] and run["status"] == "partial"
    assert plan_statuses(plan_id) == ["executed", "approved"] and paths[1].exists()
    assert main.reconcile_interrupted() == []                                                  # idempotent


def test_recovery_never_moves_files(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="a", b_dot_txt="b")
    crash_at(monkeypatch, "moved", 0)
    with pytest.raises(SimulatedCrash):
        apply(client, plan_id, ids)
    calls = []
    monkeypatch.setattr(main, "rename_no_overwrite", lambda *a: calls.append(a))
    monkeypatch.setattr(os, "rename", lambda *a: calls.append(a))
    monkeypatch.setattr(os, "replace", lambda *a: calls.append(a))
    restart()
    assert calls == []


def force_move_row(client, workspace, state="moving"):
    """A run left `moving` with both paths to be arranged by the test."""
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="aaa")
    preview = client.post(f"/api/plans/{plan_id}/preview", json={"action_ids": ids}).json()
    move = preview["moves"][0]
    connection = main.journal_connection()
    journal.new_run(connection, "crashedrun0001", plan_id, str(workspace.resolve()), 1,
                    [{"action_id": ids[0], "source": move["source"], "destination": move["destination"], "created_folder": None,
                      "size": move["size"], "mtime_ns": move["mtime_ns"], "fingerprint": move["fingerprint"]}], main.utc_now())
    journal.set_move(connection, "crashedrun0001", 0, state, main.utc_now())
    connection.close()
    return plan_id, ids, paths, Path(move["destination"])


def test_both_paths_present_is_ambiguous_and_nothing_is_touched(client, workspace):
    plan_id, ids, paths, destination = force_move_row(client, workspace)
    destination.parent.mkdir()
    destination.write_text("aaa", encoding="utf-8")
    report = restart()
    assert [r["to"] for r in report] == ["needs_attention"] and "Both" in report[0]["detail"]
    assert paths[0].read_text() == "aaa" and destination.read_text() == "aaa"
    assert only_run()["status"] == "needs_attention" and plan_statuses(plan_id) == ["approved"]


def test_a_file_at_neither_path_is_ambiguous(client, workspace):
    plan_id, ids, paths, destination = force_move_row(client, workspace)
    paths[0].unlink()
    report = restart()
    assert [r["to"] for r in report] == ["needs_attention"] and "neither" in report[0]["detail"]


def test_a_changed_file_is_ambiguous_even_if_it_is_where_it_should_be(client, workspace):
    plan_id, ids, paths, destination = force_move_row(client, workspace)
    paths[0].write_text("DIFFERENT CONTENT", encoding="utf-8")
    report = restart()
    assert [r["to"] for r in report] == ["needs_attention"] and paths[0].read_text() == "DIFFERENT CONTENT"


def test_a_destination_that_is_a_different_file_is_ambiguous(client, workspace):
    plan_id, ids, paths, destination = force_move_row(client, workspace)
    paths[0].unlink()
    destination.parent.mkdir()
    destination.write_text("zzz", encoding="utf-8")   # same size, different content
    report = restart()
    assert [r["to"] for r in report] == ["needs_attention"] and destination.read_text() == "zzz"


def test_needs_attention_runs_cannot_be_undone_and_are_visible(client, workspace):
    plan_id, ids, paths, destination = force_move_row(client, workspace)
    destination.parent.mkdir()
    destination.write_text("aaa", encoding="utf-8")
    restart()
    shown = client.get("/api/runs").json()["items"][0]
    assert shown["status"] == "needs_attention" and shown["needs_attention"] is True and shown["can_undo"] is False
    refused = client.post(f"/api/runs/{only_run()['id']}/undo")
    assert refused.status_code == 409 and "needs attention" in refused.json()["detail"]


def test_the_application_reconciles_on_startup(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="a")
    crash_at(monkeypatch, "moved", 0)
    with pytest.raises(SimulatedCrash):
        apply(client, plan_id, ids)
    monkeypatch.setattr(main, "FAULT_HOOK", None)
    main.ACTIVE_RUNS.clear()
    with TestClient(main.app):       # entering the context runs the lifespan startup
        pass
    assert only_run()["status"] == "completed" and plan_statuses(plan_id) == ["executed"]


def test_a_run_being_executed_in_this_process_is_not_reconciled_underneath_it(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="a")
    outcomes = []

    def observe():
        outcomes.append(main.reconcile_interrupted())       # a concurrent request reconciling mid-run
    crash_at(monkeypatch, "moving", 0, observe)
    with pytest.raises(SimulatedCrash):
        apply(client, plan_id, ids)
    assert outcomes == [[]]


def test_a_second_run_for_a_plan_with_a_running_run_is_refused(client, workspace):
    plan_id, ids, paths, _ = force_move_row(client, workspace)
    main.register_run("crashedrun0001")        # a live run (another thread or process), so reconciliation must leave it alone
    try:
        response = apply(client, plan_id, ids)
        assert response.status_code == 409 and "still running" in response.json()["detail"]
        assert paths[0].exists() and states("crashedrun0001") == ["moving"]
    finally:
        main.unregister_run("crashedrun0001")


# ---------------------------------------------------------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------------------------------------------------------

def run_and_id(client, workspace, **files):
    plan_id, ids, paths = approved(client, workspace, **files)
    done = apply(client, plan_id, ids)
    assert done.status_code == 200, done.text
    return plan_id, ids, paths, done.json()["run_id"]


def test_undo_restores_files_removes_the_empty_folders_it_created_and_reopens_the_plan(client, workspace):
    plan_id, ids, paths, run_id = run_and_id(client, workspace, a_dot_txt="aaa", b_dot_png="bbb")
    result = client.post(f"/api/runs/{run_id}/undo")
    body = result.json()
    assert result.status_code == 200 and body["status"] == "undone" and sorted(body["undone"]) == sorted(ids)
    assert paths[0].read_text() == "aaa" and paths[1].read_text() == "bbb"
    assert not (workspace / "documents").exists() and not (workspace / "images").exists()
    assert sorted(body["removed_folders"]) == sorted([str(workspace / "documents"), str(workspace / "images")]) and body["kept_folders"] == []
    assert "best-effort" in body["undo_note"] and "not a guarantee" in body["undo_note"]
    assert states(run_id) == ["undone", "undone"] and only_run()["status"] == "undone"
    assert plan_statuses(plan_id) == ["pending", "pending"]


def test_non_empty_created_folders_are_kept_never_deleted_recursively(client, workspace):
    plan_id, ids, paths, run_id = run_and_id(client, workspace, a_dot_txt="aaa", b_dot_png="bbb")
    keeper = workspace / "documents" / "later-user-file.txt"
    keeper.write_text("mine", encoding="utf-8")
    nested = workspace / "images" / "sub"
    nested.mkdir()
    (nested / "deep.txt").write_text("deep", encoding="utf-8")
    body = client.post(f"/api/runs/{run_id}/undo").json()
    assert body["status"] == "undone" and sorted(body["kept_folders"]) == sorted([str(workspace / "documents"), str(workspace / "images")])
    assert body["removed_folders"] == []
    assert keeper.read_text() == "mine" and (nested / "deep.txt").read_text() == "deep" and paths[0].exists() and paths[1].exists()


def test_a_folder_that_existed_before_the_run_is_never_removed(client, workspace):
    (workspace / "documents").mkdir()
    plan_id, ids, paths, run_id = run_and_id(client, workspace, a_dot_txt="aaa")
    assert only_run()["id"] == run_id
    assert read("SELECT created_folder FROM apply_moves")[0]["created_folder"] is None
    client.post(f"/api/runs/{run_id}/undo")
    assert (workspace / "documents").is_dir() and paths[0].exists()


def test_undo_goes_in_reverse_order(client, workspace, monkeypatch):
    plan_id, ids, paths, run_id = run_and_id(client, workspace, a_dot_txt="1", b_dot_txt="2", c_dot_txt="3")
    order = []
    real = main.rename_no_overwrite
    monkeypatch.setattr(main, "rename_no_overwrite", lambda s, d: (order.append(Path(d).name), real(s, d))[1])
    assert client.post(f"/api/runs/{run_id}/undo").status_code == 200
    assert order == ["c.txt", "b.txt", "a.txt"]


def test_a_partial_apply_can_be_undone(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="a", b_dot_txt="b", c_dot_txt="c")
    real = main.move_file
    monkeypatch.setattr(main, "move_file", lambda s, d: (_ for _ in ()).throw(PermissionError("x")) if s.name == "c.txt" else real(s, d))
    failed = apply(client, plan_id, ids)
    assert failed.status_code == 500
    run_id = failed.json()["detail"]["run_id"]
    assert client.get("/api/runs").json()["items"][0]["can_undo"] is True
    result = client.post(f"/api/runs/{run_id}/undo").json()
    assert result["status"] == "undone" and len(result["undone"]) == 2
    assert all(p.exists() for p in paths) and not (workspace / "documents").exists()


def test_undo_twice_is_refused(client, workspace):
    plan_id, ids, paths, run_id = run_and_id(client, workspace, a_dot_txt="a")
    assert client.post(f"/api/runs/{run_id}/undo").status_code == 200
    again = client.post(f"/api/runs/{run_id}/undo")
    assert again.status_code == 409 and "already undone" in again.json()["detail"]
    assert client.post("/api/runs/nope/undo").status_code == 404


def test_an_undone_plan_can_be_reviewed_and_applied_again(client, workspace):
    plan_id, ids, paths, run_id = run_and_id(client, workspace, a_dot_txt="a")
    client.post(f"/api/runs/{run_id}/undo")
    assert client.post(f"/api/plans/{plan_id}/approve", json={"action_ids": ids}).status_code == 200
    assert apply(client, plan_id, ids).status_code == 200 and (workspace / "documents" / "a.txt").exists()


BLOCKERS = {}


def blocker(name):
    def register(function):
        BLOCKERS[name] = function
        return function
    return register


@blocker("changed")
def _changed(workspace, paths):
    (workspace / "documents" / "a.txt").write_text("edited after the move", encoding="utf-8")
    return "changed after it was moved"


@blocker("same size different content")
def _same_size(workspace, paths):
    target = workspace / "documents" / "a.txt"
    stat = target.stat()
    target.write_text("xxx", encoding="utf-8")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    return "changed after it was moved"


@blocker("disappeared")
def _gone(workspace, paths):
    (workspace / "documents" / "a.txt").unlink()
    return "no longer where it was moved to"


@blocker("replaced by a directory")
def _dir(workspace, paths):
    (workspace / "documents" / "a.txt").unlink()
    (workspace / "documents" / "a.txt").mkdir()
    return "not a regular file"


@blocker("replaced by a symlink")
def _symlink(workspace, paths):
    target = workspace / "documents" / "a.txt"
    elsewhere = workspace.parent / "elsewhere.txt"
    elsewhere.write_text("aaa", encoding="utf-8")
    target.unlink()
    try:
        os.symlink(elsewhere, target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    return "replaced by a link"


@blocker("replaced by a junction")
def _junction(workspace, paths):
    if os.name != "nt":
        pytest.skip("junctions are a Windows feature")
    import subprocess
    target = workspace / "documents" / "a.txt"
    elsewhere = workspace.parent / "junction-target"
    elsewhere.mkdir()
    target.unlink()
    if subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(elsewhere)], capture_output=True).returncode:
        pytest.skip("cannot create a junction here")
    return "not a regular file"


@blocker("original path occupied")
def _occupied(workspace, paths):
    paths[0].write_text("someone else's file", encoding="utf-8")
    return "already exists at the original path"


@pytest.mark.parametrize("name", sorted(BLOCKERS))
def test_undo_refuses_and_changes_nothing_when_a_file_is_not_as_it_was_left(client, workspace, name):
    plan_id, ids, paths, run_id = run_and_id(client, workspace, a_dot_txt="aaa")
    reason = BLOCKERS[name](workspace, paths)
    before = sorted(str(p.relative_to(workspace.parent)) for p in workspace.parent.rglob("*"))
    contents = {p: (p.read_text() if p.is_file() and not p.is_symlink() else None) for p in workspace.parent.rglob("*")}
    response = client.post(f"/api/runs/{run_id}/undo")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert reason in detail["message"] and detail["status"] == "undo_blocked" and detail["blocked"][0]["reason"]
    assert sorted(str(p.relative_to(workspace.parent)) for p in workspace.parent.rglob("*")) == before
    assert {p: (p.read_text() if p.is_file() and not p.is_symlink() else None) for p in workspace.parent.rglob("*")} == contents
    assert states(run_id) == ["undo_blocked"] and only_run()["status"] == "undo_blocked"
    assert reason in read("SELECT detail FROM apply_moves")[0]["detail"]


def test_an_occupying_file_is_never_overwritten_or_deleted(client, workspace):
    plan_id, ids, paths, run_id = run_and_id(client, workspace, a_dot_txt="aaa")
    paths[0].write_text("PRECIOUS", encoding="utf-8")
    client.post(f"/api/runs/{run_id}/undo")
    assert paths[0].read_text() == "PRECIOUS" and (workspace / "documents" / "a.txt").read_text() == "aaa"


def test_an_original_folder_replaced_by_a_link_blocks_undo(client, workspace):
    sub = workspace / "sub"
    sub.mkdir()
    paths = make_files(sub, a_dot_txt="aaa")
    plan = client.post("/api/plans", json={"workspace_root": str(workspace), "files": [str(paths[0])]}).json()
    ids = [a["id"] for a in plan["actions"]]
    client.post(f"/api/plans/{plan['id']}/approve", json={"action_ids": ids})
    run_id = apply(client, plan["id"], ids).json()["run_id"]
    backup = workspace / "sub-moved-away"
    sub.rename(backup)
    if os.name == "nt":
        import subprocess
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(sub), str(backup)], capture_output=True, text=True).returncode == 0
    else:
        try:
            os.symlink(backup, sub, target_is_directory=True)
            made = True
        except OSError:
            made = False
    if not made:
        pytest.skip("cannot create a directory link here")
    try:
        response = client.post(f"/api/runs/{run_id}/undo")
        assert response.status_code == 409 and "original folder" in response.json()["detail"]["message"]
        assert (backup / "documents" / "a.txt").read_text() == "aaa"
    finally:
        if sub.is_junction():
            os.rmdir(sub)
        elif sub.is_symlink():
            sub.unlink()


def test_undo_is_all_or_nothing_when_only_one_file_is_blocked(client, workspace):
    plan_id, ids, paths, run_id = run_and_id(client, workspace, a_dot_txt="aaa", b_dot_txt="bbb")
    (workspace / "documents" / "b.txt").write_text("edited", encoding="utf-8")
    response = client.post(f"/api/runs/{run_id}/undo")
    assert response.status_code == 409
    assert (workspace / "documents" / "a.txt").exists() and not paths[0].exists()          # a.txt was NOT moved back on its own
    assert states(run_id) == ["moved", "undo_blocked"]


def test_a_blocked_undo_can_be_retried_after_the_problem_is_fixed(client, workspace):
    plan_id, ids, paths, run_id = run_and_id(client, workspace, a_dot_txt="aaa")
    paths[0].write_text("in the way", encoding="utf-8")
    assert client.post(f"/api/runs/{run_id}/undo").status_code == 409
    assert client.get("/api/runs").json()["items"][0]["can_undo"] is True
    paths[0].unlink()
    assert client.post(f"/api/runs/{run_id}/undo").json()["status"] == "undone"
    assert paths[0].read_text() == "aaa"


def test_undo_runs_the_workspace_policy_again(client, workspace, monkeypatch):
    plan_id, ids, paths, run_id = run_and_id(client, workspace, a_dot_txt="aaa")
    import dataclasses
    from app import workspace_policy as wp
    env = wp.current_environment()
    key = wp._keys(str(workspace.resolve()), env.windows)[0]
    monkeypatch.setattr(wp, "current_environment", lambda: dataclasses.replace(env, inside=env.inside + ((key, "credentials"),)))
    response = client.post(f"/api/runs/{run_id}/undo")
    assert response.status_code == 409 and "no longer allowed" in response.json()["detail"]["message"]
    assert (workspace / "documents" / "a.txt").exists()


def test_interrupted_undo_is_settled_from_the_file_system(client, workspace, monkeypatch):
    plan_id, ids, paths, run_id = run_and_id(client, workspace, a_dot_txt="aaa", b_dot_txt="bbb")
    crash_at(monkeypatch, "restored", 1)       # undo moves b.txt back first (reverse order), crashes before recording it
    with pytest.raises(SimulatedCrash):
        client.post(f"/api/runs/{run_id}/undo")
    assert only_run()["status"] == "undoing" and paths[1].exists()
    report = restart()
    assert [(r["from"], r["to"]) for r in report] == [("undoing", "undone")]
    assert states(run_id) == ["moved", "undone"] and only_run()["status"] == "undo_partial"
    assert client.post(f"/api/runs/{run_id}/undo").json()["status"] == "undone" and paths[0].exists() and paths[1].exists()


def test_undo_interrupted_before_the_file_moves_back_returns_to_moved(client, workspace, monkeypatch):
    plan_id, ids, paths, run_id = run_and_id(client, workspace, a_dot_txt="aaa")
    crash_at(monkeypatch, "undoing", 0)
    with pytest.raises(SimulatedCrash):
        client.post(f"/api/runs/{run_id}/undo")
    report = restart()
    assert [(r["from"], r["to"]) for r in report] == [("undoing", "moved")]
    assert only_run()["status"] == "completed" and (workspace / "documents" / "a.txt").exists()


def test_undo_failure_midway_stops_and_reports(client, workspace, monkeypatch):
    plan_id, ids, paths, run_id = run_and_id(client, workspace, a_dot_txt="1", b_dot_txt="2")
    real = main.rename_no_overwrite
    monkeypatch.setattr(main, "rename_no_overwrite", lambda s, d: (_ for _ in ()).throw(PermissionError("locked")) if Path(d).name == "a.txt" else real(s, d))
    response = client.post(f"/api/runs/{run_id}/undo")
    assert response.status_code == 500 and response.json()["detail"]["status"] == "undo_partial" and "locked" in response.json()["detail"]["message"]
    assert paths[1].exists() and (workspace / "documents" / "a.txt").exists() and not paths[0].exists()
    assert states(run_id) == ["moved", "undone"]
    assert only_run()["status"] == "undo_partial"


def test_the_runs_endpoints_describe_runs_and_always_carry_the_best_effort_note(client, workspace):
    plan_id, ids, paths, run_id = run_and_id(client, workspace, a_dot_txt="a", b_dot_txt="b")
    listing = client.get("/api/runs").json()
    assert listing["items"][0]["id"] == run_id and listing["items"][0]["counts"] == {"moved": 2} and listing["items"][0]["can_undo"] is True
    detail = client.get(f"/api/runs/{run_id}").json()
    assert [m["state"] for m in detail["moves"]] == ["moved", "moved"] and "not a guarantee" in detail["undo_note"]
    assert client.get("/api/runs/nope").status_code == 404


def test_rename_never_overwrites(tmp_path):
    source, destination = tmp_path / "s.txt", tmp_path / "d.txt"
    source.write_text("source", encoding="utf-8")
    destination.write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        main.rename_no_overwrite(source, destination)
    assert destination.read_text() == "existing" and source.read_text() == "source"
    with pytest.raises(FileExistsError):
        main.move_file(source, destination)
    free = tmp_path / "free.txt"
    main.rename_no_overwrite(source, free)
    assert free.read_text() == "source" and not source.exists()


def test_the_documented_minimum_python_matches_the_code():
    import sys
    from pathlib import Path as P
    assert main.MINIMUM_PYTHON == (3, 12) and sys.version_info >= main.MINIMUM_PYTHON
    assert hasattr(P, "is_junction")          # the reason for the minimum
    readme = (P(__file__).resolve().parent.parent.parent / "README.md").read_text(encoding="utf-8")
    assert "Python 3.12 or newer" in readme
    workflow = P(__file__).resolve().parent.parent.parent / ".github" / "workflows" / "ci.yml"
    if workflow.is_file():
        assert '["3.12", "3.13"]' in workflow.read_text(encoding="utf-8")
