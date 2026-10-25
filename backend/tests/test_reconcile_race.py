"""Crash recovery must never settle a run that is live in this process. These tests interleave recovery with Apply using
events and barriers (no sleeps to "hope" for an ordering). Synthetic workspaces and temporary databases only."""
import json
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import journal, main

WAIT = 20  # seconds: only a failsafe so a broken test fails instead of hanging; every ordering below is forced by events


class SimulatedCrash(BaseException):
    pass


@pytest.fixture(autouse=True)
def clean_hooks(monkeypatch):
    monkeypatch.setattr(journal, "RECONCILE_HOOKS", {})
    yield
    main.FAULT_HOOK = None
    main.ACTIVE_RUNS.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


def workspace_in(fake_profile, name: str) -> Path:
    root = fake_profile.profile / "Documents" / name
    root.mkdir()
    return root


def approved(client, root: Path, **files: str):
    paths = []
    for name, text in files.items():
        path = root / name.replace("_dot_", ".")
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    plan = client.post("/api/plans", json={"workspace_root": str(root), "files": [str(p) for p in paths]}).json()
    ids = [a["id"] for a in plan["actions"]]
    assert client.post(f"/api/plans/{plan['id']}/approve", json={"action_ids": ids}).status_code == 200
    token = client.post(f"/api/plans/{plan['id']}/preview", json={"action_ids": ids}).json()["confirmation_token"]
    return plan["id"], ids, paths, token


def db_rows(sql: str, *args):
    connection = sqlite3.connect(main.DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, args).fetchall()
    finally:
        connection.close()


def states(run_id: str) -> list[str]:
    return [r["state"] for r in db_rows("SELECT state FROM apply_moves WHERE run_id = ? ORDER BY seq", run_id)]


class Worker(threading.Thread):
    """Runs a callable in its own thread and keeps the result or the exception for the test to assert on."""
    def __init__(self, target):
        super().__init__(daemon=True)
        self.target, self.result, self.error = target, None, None

    def run(self):
        try:
            self.result = self.target()
        except BaseException as error:  # noqa: BLE001
            self.error = error

    def finish(self):
        self.join(WAIT)
        assert not self.is_alive(), "deadlock: a worker did not finish"
        assert self.error is None, self.error
        return self.result


def apply_in_thread(plan_id, ids, token) -> Worker:
    def go():
        return TestClient(main.app).post("/api/execute", json={"plan_id": plan_id, "action_ids": ids, "confirmation_token": token})
    worker = Worker(go)
    worker.start()
    return worker


def pause_apply_at(point: str, seq: int):
    """Make Apply stop at a crash-injection point until released; returns (reached, release)."""
    reached, release = threading.Event(), threading.Event()
    apply_thread = {}

    def hook(where, at):
        if where == point and at == seq and not reached.is_set():   # one-shot; TestClient runs the app on its own threads
            reached.set()
            assert release.wait(WAIT)
    main.FAULT_HOOK = hook
    return reached, release, apply_thread


# ---------------------------------------------------------------------------------------------------------------------------
# The reported race: Apply starts after recovery began, before recovery read the runs
# ---------------------------------------------------------------------------------------------------------------------------

def test_apply_starting_just_before_recovery_reads_the_runs_is_not_settled_underneath(client, fake_profile):
    root = workspace_in(fake_profile, "live")
    plan_id, ids, paths, token = approved(client, root, a_dot_txt="aaa", b_dot_txt="bbb")
    at_read, go_read = threading.Event(), threading.Event()
    recovery_thread = {}

    def before_read():                       # the point where the OLD code had already taken its snapshot of live runs
        if threading.current_thread() is recovery_thread.get("t"):
            at_read.set()
            assert go_read.wait(WAIT)
    journal.RECONCILE_HOOKS["before_read"] = before_read
    recovery = Worker(main.reconcile_interrupted)
    recovery_thread["t"] = recovery
    recovery.start()
    assert at_read.wait(WAIT)                # recovery is parked right before it reads the database

    reached, release, apply_thread = pause_apply_at("planned", -1)   # Apply registers, commits its run row, and parks
    applying = apply_in_thread(plan_id, ids, token)
    apply_thread["t"] = applying
    assert reached.wait(WAIT)                # the run row now exists and the run is live, but nothing has moved yet
    assert [r["status"] for r in db_rows("SELECT status FROM apply_runs")] == ["running"]

    go_read.set()                            # recovery now reads the database and sees the live run's row
    report = recovery.finish()
    assert report == []                      # it was left alone: not skipped, not failed, not completed
    run = db_rows("SELECT * FROM apply_runs")[0]
    assert run["status"] == "running" and run["finished_at"] is None and run["summary"] == ""
    assert states(run["id"]) == ["planned", "planned"] and all(p.exists() for p in paths)

    release.set()                            # Apply continues and finishes normally
    response = applying.finish()
    assert response.status_code == 200 and response.json()["status"] == "completed"
    run = db_rows("SELECT * FROM apply_runs")[0]
    assert run["status"] == "completed" and states(run["id"]) == ["moved", "moved"]
    assert [(root / "documents" / n).read_text() for n in ("a.txt", "b.txt")] == ["aaa", "bbb"]
    assert not any(p.exists() for p in paths)
    assert main.reconcile_interrupted() == []


def test_the_run_stays_consistent_with_the_file_system_if_recovery_runs_mid_move(client, fake_profile):
    root = workspace_in(fake_profile, "midmove")
    plan_id, ids, paths, token = approved(client, root, a_dot_txt="aaa", b_dot_txt="bbb")
    reached, release, apply_thread = pause_apply_at("moving", 1)    # first file moved and recorded; second is `moving`
    applying = apply_in_thread(plan_id, ids, token)
    apply_thread["t"] = applying
    assert reached.wait(WAIT)
    run_id = db_rows("SELECT id FROM apply_runs")[0]["id"]
    assert states(run_id) == ["moved", "moving"] and (root / "documents" / "a.txt").exists() and paths[1].exists()
    for _ in range(3):
        assert main.reconcile_interrupted() == []                    # recovery runs while the run is live, three times
    assert states(run_id) == ["moved", "moving"] and paths[1].exists()
    release.set()
    assert applying.finish().status_code == 200
    assert states(run_id) == ["moved", "moved"] and (root / "documents" / "b.txt").read_text() == "bbb"


# ---------------------------------------------------------------------------------------------------------------------------
# A run that finishes between recovery's read and recovery's check
# ---------------------------------------------------------------------------------------------------------------------------

def test_a_run_that_finishes_after_recovery_read_it_is_not_overwritten(client, fake_profile):
    root = workspace_in(fake_profile, "finishing")
    plan_id, ids, paths, token = approved(client, root, a_dot_txt="aaa")
    reached, release, apply_thread = pause_apply_at("planned", -1)
    applying = apply_in_thread(plan_id, ids, token)
    apply_thread["t"] = applying
    assert reached.wait(WAIT)                                         # live run, row visible as `running`

    read_done, go_check = threading.Event(), threading.Event()
    recovery_thread = {}

    def after_read():
        if threading.current_thread() is recovery_thread.get("t"):
            read_done.set()
            assert go_check.wait(WAIT)
    journal.RECONCILE_HOOKS["after_read"] = after_read
    recovery = Worker(main.reconcile_interrupted)
    recovery_thread["t"] = recovery
    recovery.start()
    assert read_done.wait(WAIT)                                       # recovery holds a stale view that says `running`
    release.set()
    assert applying.finish().status_code == 200                       # the run completes and deregisters
    go_check.set()                                                    # now recovery checks: the run is no longer active...
    assert recovery.finish() == []                                    # ...but it is also no longer running, so it is left alone
    run = db_rows("SELECT * FROM apply_runs")[0]
    assert run["status"] == "completed" and run["summary"] == "Moved 1 file(s)."
    assert states(run["id"]) == ["moved"] and (root / "documents" / "a.txt").exists()


# ---------------------------------------------------------------------------------------------------------------------------
# A genuinely interrupted run is still settled, even while another run is live
# ---------------------------------------------------------------------------------------------------------------------------

def test_an_interrupted_inactive_run_is_settled_while_a_live_run_is_left_alone(client, fake_profile):
    dead_root, live_root = workspace_in(fake_profile, "dead"), workspace_in(fake_profile, "alive")
    live_plan, live_ids, live_paths, live_token = approved(client, live_root, c_dot_txt="ccc")
    reached, release, apply_thread = pause_apply_at("planned", -1)
    applying = apply_in_thread(live_plan, live_ids, live_token)
    apply_thread["t"] = applying
    assert reached.wait(WAIT)                                         # the live run is registered and parked
    live_run = db_rows("SELECT id FROM apply_runs")[0]["id"]

    # A second run that died mid-move: its file already sits at the destination, the journal says `moving`, nobody executes it.
    dead_plan, dead_ids, dead_paths, _ = approved(client, dead_root, a_dot_txt="aaa", b_dot_txt="bbb")
    previews = client.post(f"/api/plans/{dead_plan}/preview", json={"action_ids": dead_ids}).json()["moves"]
    connection = main.journal_connection()
    journal.new_run(connection, "deadrun00000001", dead_plan, str(dead_root.resolve()), 1,
                    [{"action_id": m["action_id"], "source": m["source"], "destination": m["destination"], "created_folder": None,
                      "size": m["size"], "mtime_ns": m["mtime_ns"], "fingerprint": m["fingerprint"]} for m in previews], main.utc_now())
    journal.set_move(connection, "deadrun00000001", 0, "moving", main.utc_now())
    connection.close()
    first = Path(previews[0]["destination"])
    first.parent.mkdir()
    dead_paths[0].rename(first)                                       # the rename happened; the journal update never did

    report = main.reconcile_interrupted()
    assert sorted((r["run_id"], r["from"], r["to"]) for r in report) == sorted(
        [("deadrun00000001", "moving", "moved"), ("deadrun00000001", "planned", "skipped")])
    assert states("deadrun00000001") == ["moved", "skipped"] and dead_paths[1].exists() and first.read_text() == "aaa"
    assert db_rows("SELECT status FROM apply_runs WHERE id = 'deadrun00000001'")[0]["status"] == "partial"
    assert states(live_run) == ["planned"] and live_paths[0].exists()          # the live run was not touched

    release.set()
    assert applying.finish().status_code == 200
    assert states(live_run) == ["moved"] and (live_root / "documents" / "c.txt").read_text() == "ccc"
    assert main.reconcile_interrupted() == []                                   # idempotent: nothing left to settle
    assert main.reconcile_interrupted() == []


# ---------------------------------------------------------------------------------------------------------------------------
# No deadlock, no interference, idempotence
# ---------------------------------------------------------------------------------------------------------------------------

def test_concurrent_recovery_and_apply_do_not_deadlock_or_interfere(client, fake_profile):
    root = workspace_in(fake_profile, "storm")
    plan_id, ids, paths, token = approved(client, root, a_dot_txt="a", b_dot_txt="b", c_dot_txt="c")
    reached, release, apply_thread = pause_apply_at("moving", 1)
    applying = apply_in_thread(plan_id, ids, token)
    apply_thread["t"] = applying
    assert reached.wait(WAIT)                                         # Apply holds the plan lock and is live

    start = threading.Barrier(5)
    reports: list = []

    def hammer():
        start.wait(WAIT)
        for _ in range(15):
            reports.append(main.reconcile_interrupted())
            TestClient(main.app).get("/api/runs")                     # other callers that reconcile lazily
    crowd = [Worker(hammer) for _ in range(4)]
    for worker in crowd:
        worker.start()
    start.wait(WAIT)
    for worker in crowd:
        worker.finish()                                               # all four finish while Apply is still parked: no deadlock
    assert reports and all(report == [] for report in reports)

    release.set()
    assert applying.finish().status_code == 200
    run_id = db_rows("SELECT id FROM apply_runs")[0]["id"]
    assert states(run_id) == ["moved", "moved", "moved"] and db_rows("SELECT status FROM apply_runs")[0]["status"] == "completed"
    assert sorted(p.name for p in (root / "documents").iterdir()) == ["a.txt", "b.txt", "c.txt"]


def test_recovery_and_a_second_apply_attempt_cannot_deadlock_on_the_plan_lock(client, fake_profile):
    root = workspace_in(fake_profile, "locks")
    plan_id, ids, paths, token = approved(client, root, a_dot_txt="a")
    reached, release, apply_thread = pause_apply_at("moving", 0)
    applying = apply_in_thread(plan_id, ids, token)
    apply_thread["t"] = applying
    assert reached.wait(WAIT)
    second = Worker(lambda: TestClient(main.app).post("/api/execute", json={"plan_id": plan_id, "action_ids": ids, "confirmation_token": "x"}))
    third = Worker(main.reconcile_interrupted)
    second.start(); third.start()
    assert second.finish().status_code == 409 and third.finish() == []
    release.set()
    assert applying.finish().status_code == 200


def test_the_active_registry_is_registered_before_the_row_and_released_after(client, fake_profile):
    root = workspace_in(fake_profile, "order")
    plan_id, ids, paths, token = approved(client, root, a_dot_txt="a")
    observed = {}

    def hook(where, at):
        if where == "planned":
            run_id = db_rows("SELECT id FROM apply_runs")[0]["id"]
            observed["registered_when_row_exists"] = main.is_run_active(run_id)
            observed["run_id"] = run_id
    main.FAULT_HOOK = hook
    assert client.post("/api/execute", json={"plan_id": plan_id, "action_ids": ids, "confirmation_token": token}).status_code == 200
    assert observed["registered_when_row_exists"] is True and not main.is_run_active(observed["run_id"]) and main.ACTIVE_RUNS == set()


def test_recovery_is_idempotent_and_changes_nothing_on_repeat(client, fake_profile):
    root = workspace_in(fake_profile, "idem")
    plan_id, ids, paths, token = approved(client, root, a_dot_txt="a", b_dot_txt="b")

    def crash(where, at):
        if where == "moved" and at == 0:
            raise SimulatedCrash(where)
    main.FAULT_HOOK = crash
    with pytest.raises(SimulatedCrash):
        client.post("/api/execute", json={"plan_id": plan_id, "action_ids": ids, "confirmation_token": token})
    main.FAULT_HOOK = None
    main.ACTIVE_RUNS.clear()
    first = main.reconcile_interrupted()
    snapshot = [dict(r) for r in db_rows("SELECT * FROM apply_moves ORDER BY seq")], [dict(r) for r in db_rows("SELECT * FROM apply_runs")]
    assert len(first) == 2
    for _ in range(3):
        assert main.reconcile_interrupted() == []
    assert ([dict(r) for r in db_rows("SELECT * FROM apply_moves ORDER BY seq")], [dict(r) for r in db_rows("SELECT * FROM apply_runs")]) == snapshot
    assert json.loads(db_rows("SELECT actions FROM plans")[0]["actions"])[0]["status"] == "executed"


def test_the_registry_uses_a_leaf_lock_only():
    import inspect
    for function in (main.register_run, main.unregister_run, main.is_run_active):
        source = inspect.getsource(function)
        assert "with _ACTIVE_LOCK" in source and source.count("with ") == 1 and "plan_lock" not in source and "connection" not in source
