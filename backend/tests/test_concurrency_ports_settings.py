"""Phase 4D/E: locking and double-Apply, the explicit deterministic-fallback setting (F7), and non-default port configuration.
Synthetic workspaces and databases only; the port tests start a throw-away server on free local ports with a temp database."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import api_guard, main
from app.api_guard import Guard, TOKEN_HEADER

BACKEND = Path(__file__).resolve().parent.parent


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


@pytest.fixture
def workspace(fake_profile) -> Path:
    root = fake_profile.profile / "Documents" / "ws"
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
    return plan["id"], ids, paths


def preview_token(client, plan_id, ids) -> str:
    response = client.post(f"/api/plans/{plan_id}/preview", json={"action_ids": ids})
    assert response.status_code == 200, response.text
    return response.json()["confirmation_token"]


def run_count() -> int:
    connection = sqlite3.connect(main.DB_PATH)
    try:
        return connection.execute("SELECT COUNT(*) FROM apply_runs").fetchone()[0]
    finally:
        connection.close()


def slow_moves(monkeypatch, seconds=0.4, started: threading.Event | None = None):
    real = main.move_file

    def slow(source, destination):
        if started:
            started.set()
        time.sleep(seconds)
        real(source, destination)
    monkeypatch.setattr(main, "move_file", slow)


# ---------------------------------------------------------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------------------------------------------------------

def test_concurrent_double_apply_exactly_one_wins_and_nothing_is_duplicated(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="aaa", b_dot_txt="bbb")
    first, second = preview_token(client, plan_id, ids), preview_token(client, plan_id, ids)   # two valid confirmations
    slow_moves(monkeypatch)
    results: list[tuple[int, dict]] = []
    barrier = threading.Barrier(2)

    def attempt(token):
        local = TestClient(main.app)
        barrier.wait()
        response = local.post("/api/execute", json={"plan_id": plan_id, "action_ids": ids, "confirmation_token": token})
        results.append((response.status_code, response.json()))
    threads = [threading.Thread(target=attempt, args=(token,)) for token in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(code for code, _ in results) == [200, 409], results
    loser = next(body for code, body in results if code == 409)
    assert "in progress" in loser["detail"] or "since the preview" in loser["detail"]
    assert run_count() == 1
    assert (workspace / "documents" / "a.txt").read_text() == "aaa" and (workspace / "documents" / "b.txt").read_text() == "bbb"
    assert sorted(p.name for p in workspace.rglob("*") if p.is_file()) == ["a.txt", "b.txt"]
    assert not any(p.exists() for p in paths)


def test_a_second_apply_while_one_is_running_is_refused_immediately_without_waiting(client, workspace, monkeypatch):
    plan_id, ids, _ = approved(client, workspace, a_dot_txt="aaa")
    token = preview_token(client, plan_id, ids)
    started = threading.Event()
    slow_moves(monkeypatch, 1.0, started)
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("first", TestClient(main.app).post(
        "/api/execute", json={"plan_id": plan_id, "action_ids": ids, "confirmation_token": token})))
    worker.start()
    assert started.wait(10)
    begun = time.monotonic()
    second = TestClient(main.app).post("/api/execute", json={"plan_id": plan_id, "action_ids": ids, "confirmation_token": "whatever"})
    assert second.status_code == 409 and "in progress" in second.json()["detail"] and time.monotonic() - begun < 0.8
    worker.join(30)
    assert outcome["first"].status_code == 200 and run_count() == 1


def test_an_edit_during_apply_waits_and_then_cannot_change_an_executed_action(client, workspace, monkeypatch):
    plan_id, ids, paths = approved(client, workspace, a_dot_txt="aaa")
    token = preview_token(client, plan_id, ids)
    started = threading.Event()
    slow_moves(monkeypatch, 0.6, started)
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("apply", TestClient(main.app).post(
        "/api/execute", json={"plan_id": plan_id, "action_ids": ids, "confirmation_token": token})))
    worker.start()
    assert started.wait(10)
    edit = TestClient(main.app).patch(f"/api/plans/{plan_id}/actions/{ids[0]}", json={"proposed_name": "late.txt"})
    worker.join(30)
    assert outcome["apply"].status_code == 200
    assert edit.status_code == 409 and "already been executed" in edit.json()["detail"]
    assert (workspace / "documents" / "a.txt").exists() and not (workspace / "documents" / "late.txt").exists()


def test_the_lock_is_per_plan_so_different_plans_do_not_block_each_other(client, workspace, fake_profile):
    plan_a, ids_a, _ = approved(client, workspace, a_dot_txt="a")
    other = fake_profile.profile / "Documents" / "other"
    other.mkdir()
    plan_b, ids_b, _ = approved(client, other, b_dot_txt="b")
    with main.plan_lock(plan_a, wait_seconds=0):
        assert client.post(f"/api/plans/{plan_b}/preview", json={"action_ids": ids_b}).status_code == 200
        busy = client.post("/api/execute", json={"plan_id": plan_a, "action_ids": ids_a, "confirmation_token": "x"})
        assert busy.status_code == 409 and "in progress" in busy.json()["detail"]


def test_edit_and_approve_racing_never_approve_an_unreviewed_edit(client, workspace):
    plan = client.post("/api/plans", json={"workspace_root": str(workspace), "files": [str(workspace / "a.txt")]})
    (workspace / "a.txt").write_text("aaa", encoding="utf-8")
    plan = client.post("/api/plans", json={"workspace_root": str(workspace), "files": [str(workspace / "a.txt")]}).json()
    action = plan["actions"][0]
    reviewed = {action["id"]: action["revision"]}
    outcomes: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def edit():
        barrier.wait()
        outcomes["edit"] = TestClient(main.app).patch(f"/api/plans/{plan['id']}/actions/{action['id']}", json={"proposed_name": "edited.txt"}).status_code

    def approve():
        barrier.wait()
        outcomes["approve"] = TestClient(main.app).post(f"/api/plans/{plan['id']}/approve", json={"action_ids": [action["id"]], "revisions": reviewed}).status_code
    threads = [threading.Thread(target=edit), threading.Thread(target=approve)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    connection = sqlite3.connect(main.DB_PATH)
    stored = json.loads(connection.execute("SELECT actions FROM plans WHERE id = ?", (plan["id"],)).fetchone()[0])[0]
    connection.close()
    assert outcomes["edit"] == 200
    if outcomes["approve"] == 200:        # approve won the race: it was applied to the version that was reviewed, then the edit voided it
        assert stored["status"] == "pending" and stored["proposed_name"] == "edited.txt"
    else:                                 # the edit won: the stale approval was refused
        assert outcomes["approve"] == 409 and stored["status"] == "pending"
    assert stored["proposed_name"] == "edited.txt"


def test_many_concurrent_previews_and_edits_keep_the_plan_consistent(client, workspace):
    plan_id, ids, _ = approved(client, workspace, a_dot_txt="a", b_dot_txt="b")
    errors = []

    def hammer(index):
        local = TestClient(main.app)
        for step in range(5):
            response = local.patch(f"/api/plans/{plan_id}/actions/{ids[index % 2]}", json={"proposed_name": f"n{index}-{step}.txt"})
            if response.status_code != 200:
                errors.append(response.text)
    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors
    connection = sqlite3.connect(main.DB_PATH)
    items = json.loads(connection.execute("SELECT actions FROM plans WHERE id = ?", (plan_id,)).fetchone()[0])
    revision = connection.execute("SELECT revision FROM plan_state WHERE plan_id = ?", (plan_id,)).fetchone()[0]
    connection.close()
    assert sum(item["revision"] for item in items) >= 20 and revision >= 20     # no lost updates


# ---------------------------------------------------------------------------------------------------------------------------
# F7: an explicit "Deterministic fallback" can always be saved
# ---------------------------------------------------------------------------------------------------------------------------

def test_explicit_deterministic_fallback_persists_even_when_the_default_is_not_installed(client):
    before = client.get("/api/settings").json()
    assert before["saved_choice"] is None and before["source"] == "default" and before["model"] == main.DEFAULT_OLLAMA_MODEL
    assert client.patch("/api/settings", json={"model": None}).status_code == 200      # no model is installed; selecting "none" must still save
    after = client.get("/api/settings").json()
    assert after["saved_choice"] == "none" and after["source"] == "none" and after["model"] is None
    assert main.ollama_model_choice() == (None, "none")


def test_saving_deterministic_fallback_twice_is_idempotent_and_survives_a_model_choice(client, monkeypatch):
    client.patch("/api/settings", json={"model": None})
    client.patch("/api/settings", json={"model": None})
    assert client.get("/api/settings").json()["saved_choice"] == "none"
    monkeypatch.setattr(main, "ollama_models", lambda: ["qwen2.5:3b", "other:1b"])
    assert client.patch("/api/settings", json={"model": "other:1b"}).status_code == 200
    chosen = client.get("/api/settings").json()
    assert chosen["saved_choice"] == "model" and chosen["model"] == "other:1b" and chosen["source"] == "setting"
    client.patch("/api/settings", json={"model": None})
    assert main.ollama_model_choice() == (None, "none")


def test_explicit_none_beats_the_default_but_not_the_environment_variable(client, monkeypatch):
    client.patch("/api/settings", json={"model": None})
    monkeypatch.setenv("LAZYDESK_DEFAULT_MODEL", "something:7b")
    assert main.ollama_model_choice() == (None, "none")
    monkeypatch.setenv("OLLAMA_MODEL", "env-model")
    assert main.ollama_model_choice() == ("env-model", "environment")     # existing precedence is unchanged


# ---------------------------------------------------------------------------------------------------------------------------
# E: non-default backend port
# ---------------------------------------------------------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_non_default_backend_port_works_in_process_with_the_right_host_origin_and_token(raw_client_factory, workspace, monkeypatch):
    guard = Guard(backend_port=9123, extra_peers=("testclient",))
    monkeypatch.setattr(main, "GUARD", guard)
    raw = raw_client_factory()
    origin = {"origin": "http://127.0.0.1:5173"}
    assert raw.get("/api/health", headers={"host": "127.0.0.1:9123"}).status_code == 200
    assert raw.get("/api/health", headers={"host": "127.0.0.1:8000"}).status_code == 400            # the old default no longer works
    token = raw.post("/api/session/bootstrap", headers={"host": "127.0.0.1:9123", **origin}).json()["token"]
    assert token == guard.token
    headers = {"host": "127.0.0.1:9123", TOKEN_HEADER: token, **origin}
    (workspace / "a.txt").write_text("x", encoding="utf-8")
    scan = raw.post("/api/scan", json={"path": str(workspace)}, headers=headers)
    assert scan.status_code == 200 and scan.json()["count"] == 1
    plan = raw.post("/api/plans", json={"workspace_root": str(workspace), "files": [str(workspace / "a.txt")]}, headers=headers).json()
    ids = [a["id"] for a in plan["actions"]]
    assert raw.post(f"/api/plans/{plan['id']}/approve", json={"action_ids": ids}, headers=headers).status_code == 200
    preview = raw.post(f"/api/plans/{plan['id']}/preview", json={"action_ids": ids}, headers=headers).json()
    done = raw.post("/api/execute", json={"plan_id": plan["id"], "action_ids": ids, "confirmation_token": preview["confirmation_token"]}, headers=headers)
    assert done.status_code == 200 and (workspace / "documents" / "a.txt").exists()
    assert raw.post("/api/scan", json={"path": str(workspace)}, headers={**headers, "host": "127.0.0.1:8000"}).status_code == 400


def http(method: str, port: int, path: str, headers: dict | None = None, body: bytes | None = None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=headers or {}, data=body)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(), dict(error.headers)


def start_server(env_port: int | str, listen_port: int, frontend_port: int | None, tmp_path: Path):
    env = {**os.environ, "LAZYDESK_PORT": str(env_port), "LAZYDESK_DB": str(tmp_path / "server.sqlite"), "PYTHONDONTWRITEBYTECODE": "1"}
    if frontend_port:
        env["LAZYDESK_FRONTEND_PORT"] = str(frontend_port)
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(listen_port)],
                               cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            http("GET", listen_port, "/api/health")
            return process
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.2)
    process.kill()
    raise AssertionError("server did not start: " + process.communicate()[0])


def stop_server(process) -> str:
    process.terminate()
    try:
        return process.communicate(timeout=15)[0]
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate()[0]


def test_a_real_server_on_non_default_ports_serves_the_whole_authorized_flow(tmp_path):
    backend, frontend = free_port(), free_port()
    server = start_server(backend, backend, frontend, tmp_path)
    try:
        origin = f"http://127.0.0.1:{frontend}"
        assert http("GET", backend, "/api/health")[0:2] == (200, '{"status":"ok"}')
        assert http("POST", backend, "/api/session/bootstrap", {"Origin": "http://127.0.0.1:5173"})[0] == 403    # the DEFAULT frontend origin is not accepted now
        status, body, headers = http("POST", backend, "/api/session/bootstrap", {"Origin": origin})
        assert status == 200 and {k.lower(): v for k, v in headers.items()}["access-control-allow-origin"] == origin
        token = json.loads(body)["token"]
        authorized = {TOKEN_HEADER: token, "Origin": origin}
        assert http("GET", backend, "/api/activity", authorized)[0] == 200
        assert http("GET", backend, "/api/activity", {"Origin": origin})[0] == 401
        assert http("GET", backend, "/api/activity", {**authorized, "Host": "127.0.0.1:8000"})[0] == 400
        assert http("GET", backend, "/api/runs", authorized)[0] == 200
    finally:
        log = stop_server(server)
    assert token not in log


def test_a_server_listening_on_a_different_port_than_configured_rejects_requests_and_explains_why(tmp_path):
    configured, actual = free_port(), free_port()
    server = start_server(configured, actual, None, tmp_path)
    try:
        assert http("GET", actual, "/api/health")[0] == 400                      # Host port != LAZYDESK_PORT
    finally:
        log = stop_server(server)
    assert "LAZYDESK_PORT" in log and str(configured) in log and str(actual) in log


@pytest.mark.parametrize("value", ["", "abc", "0", "70000", "-5", "8 0"])
def test_the_backend_refuses_to_start_with_an_unsupported_port_setting(tmp_path, value):
    env = {**os.environ, "LAZYDESK_PORT": value, "LAZYDESK_DB": str(tmp_path / "x.sqlite"), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-c", "import app.main"], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode != 0 and "LAZYDESK_PORT must be a whole number from 1 to 65535" in result.stderr
