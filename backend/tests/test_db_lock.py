"""One backend per database. Real child processes, temporary directories and temporary databases only: nothing here can touch
the real database, the demo database, or any running demo process (child processes are tracked and only those are stopped)."""
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db_lock, journal, main
from app.db_lock import DatabaseLock, DatabaseLockError, lock_path_for

BACKEND = Path(__file__).resolve().parent.parent
WAIT = 60  # failsafe only; orderings below are forced by pipes and process exits

HOLDER = r"""
import sys
sys.path.insert(0, sys.argv[1])
from app.db_lock import DatabaseLock
lock = DatabaseLock(sys.argv[2]).acquire()
print("LOCKED", flush=True)
sys.stdin.readline()          # the parent decides when this process exits normally
lock.release()
print("RELEASED", flush=True)
"""


@pytest.fixture
def children():
    started: list[subprocess.Popen] = []
    yield started
    for process in started:           # only processes started by this test
        if process.poll() is None:
            process.kill()
        process.communicate()


def hold(database: Path, children) -> subprocess.Popen:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.Popen([sys.executable, "-I", "-c", HOLDER, str(BACKEND), str(database)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env=env)
    children.append(process)
    assert process.stdout.readline().strip() == "LOCKED"      # blocks until the child really owns the lock
    return process


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def http_get(port: int, path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, ""


def backend(database: Path, port: int, children) -> subprocess.Popen:
    env = {**os.environ, "LAZYDESK_PORT": str(port), "LAZYDESK_DB": str(database), "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)], cwd=BACKEND, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    children.append(process)
    return process


def wait_until_serving(process: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        assert process.poll() is None, "backend exited early"
        try:
            if http_get(port, "/api/health")[0] == 200:
                return
        except (urllib.error.URLError, OSError):
            pass
    raise AssertionError("backend did not start")


def seed_interrupted_run(database: Path) -> None:
    """A run a crashed process left behind: `running`, with planned moves. Recovery would mark it failed / its moves skipped."""
    connection = main.db(database)
    connection.close()
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    journal.new_run(connection, "seededrun000001", "plan0", "C:/synthetic/ws", 1,
                    [{"action_id": "a", "source": "C:/synthetic/ws/a.txt", "destination": "C:/synthetic/ws/documents/a.txt", "size": 1, "mtime_ns": 1}], "2026-01-01T00:00:00+00:00")
    connection.close()


def journal_snapshot(database: Path):
    connection = sqlite3.connect(database)
    try:
        return (connection.execute("SELECT id, status, summary FROM apply_runs").fetchall(),
                connection.execute("SELECT run_id, seq, state, detail FROM apply_moves").fetchall())
    finally:
        connection.close()


# ---------------------------------------------------------------------------------------------------------------------------
# The lock itself
# ---------------------------------------------------------------------------------------------------------------------------

def test_a_process_can_acquire_and_release_the_database_lock(tmp_path):
    database = tmp_path / "a.sqlite"
    lock = DatabaseLock(database)
    assert not lock.held
    assert lock.acquire() is lock and lock.held and lock.path == tmp_path / "a.sqlite.lock"
    assert lock.path.exists() and lock.acquire() is lock          # acquiring twice is harmless
    lock.release()
    lock.release()                                                 # releasing twice is harmless
    assert not lock.held
    assert (tmp_path / "a.sqlite.lock").exists()                   # the file is never deleted: the OS lock is the ownership
    with DatabaseLock(database) as again:
        assert again.held


def test_a_second_holder_in_the_same_process_is_refused_until_release(tmp_path):
    database = tmp_path / "a.sqlite"
    first = DatabaseLock(database).acquire()
    with pytest.raises(DatabaseLockError, match="Only one backend may use a database at a time"):
        DatabaseLock(database).acquire()
    first.release()
    DatabaseLock(database).acquire().release()


def test_a_second_real_process_is_refused_while_another_process_holds_the_lock(tmp_path, children):
    database = tmp_path / "shared.sqlite"
    owner = hold(database, children)
    with pytest.raises(DatabaseLockError) as refused:
        DatabaseLock(database).acquire()
    message = str(refused.value)
    assert "Another LazyDesk backend" in message and "LAZYDESK_DB" in message and "Nothing was changed" in message
    other = subprocess.run([sys.executable, "-I", "-c", HOLDER.replace('sys.stdin.readline()', ''), str(BACKEND), str(database)],
                           capture_output=True, text=True, timeout=WAIT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert other.returncode != 0 and "Only one backend may use a database at a time" in other.stderr and "LOCKED" not in other.stdout
    owner.stdin.write("quit\n"); owner.stdin.flush()


def test_the_lock_is_available_after_the_owner_exits_normally(tmp_path, children):
    database = tmp_path / "shared.sqlite"
    owner = hold(database, children)
    with pytest.raises(DatabaseLockError):
        DatabaseLock(database).acquire()
    owner.stdin.write("quit\n"); owner.stdin.flush()
    assert owner.stdout.readline().strip() == "RELEASED"
    assert owner.wait(WAIT) == 0
    DatabaseLock(database).acquire().release()
    assert (tmp_path / "shared.sqlite.lock").exists()              # still there; ownership never depended on it


def test_the_lock_is_available_after_the_owner_is_killed_without_cleanup(tmp_path, children):
    database = tmp_path / "shared.sqlite"
    owner = hold(database, children)
    owner.kill()                                                   # no release, no shutdown hooks: a crash
    owner.wait(WAIT)
    DatabaseLock(database).acquire().release()                     # no manual deletion of anything was needed
    assert (tmp_path / "shared.sqlite.lock").exists()


def test_different_databases_are_locked_independently(tmp_path, children):
    one, two = tmp_path / "one.sqlite", tmp_path / "two.sqlite"
    hold(one, children)
    with DatabaseLock(two):                                        # a different database is not affected
        with pytest.raises(DatabaseLockError):
            DatabaseLock(one).acquire()
    sub = tmp_path / "sub"
    sub.mkdir()
    with DatabaseLock(sub / "one.sqlite"):                         # same file name, different folder: independent
        pass


def test_every_spelling_of_one_database_path_maps_to_one_lock_file(tmp_path):
    (tmp_path / "d").mkdir()
    spellings = [tmp_path / "d" / "x.sqlite", tmp_path / "d" / ".." / "d" / "x.sqlite", Path(os.path.relpath(tmp_path / "d" / "x.sqlite"))
                 if os.path.splitdrive(str(tmp_path))[0] == os.path.splitdrive(os.getcwd())[0] else tmp_path / "d" / "x.sqlite"]
    assert len({lock_path_for(path) for path in spellings}) == 1
    first = DatabaseLock(spellings[0]).acquire()
    for other in spellings[1:]:
        with pytest.raises(DatabaseLockError):
            DatabaseLock(other).acquire()
    first.release()


@pytest.mark.parametrize("case", ["missing_folder", "database_is_directory", "lock_path_is_directory", "permission_denied", "nul_in_path"])
def test_an_unusable_path_or_failed_acquisition_refuses_instead_of_running_unprotected(tmp_path, monkeypatch, case):
    if case == "missing_folder":
        target = tmp_path / "nope" / "x.sqlite"
    elif case == "database_is_directory":
        (tmp_path / "x.sqlite").mkdir()
        target = tmp_path / "x.sqlite"
    elif case == "lock_path_is_directory":
        (tmp_path / "x.sqlite.lock").mkdir()
        target = tmp_path / "x.sqlite"
    elif case == "nul_in_path":
        target = str(tmp_path / "x\x00.sqlite")
    else:
        target = tmp_path / "x.sqlite"
        monkeypatch.setattr(os, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "Access is denied")))
    with pytest.raises(DatabaseLockError) as error:
        DatabaseLock(target).acquire()
    assert "refusing to start" in str(error.value) or "refusing" in str(error.value)


def test_a_failing_operating_system_lock_is_never_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(db_lock, "_lock", lambda fd: (_ for _ in ()).throw(OSError(95, "not supported on this filesystem")))
    lock = DatabaseLock(tmp_path / "x.sqlite")
    with pytest.raises(DatabaseLockError):
        lock.acquire()
    assert not lock.held


# ---------------------------------------------------------------------------------------------------------------------------
# Startup ordering and the real backend
# ---------------------------------------------------------------------------------------------------------------------------

def test_application_startup_locks_before_recovery_and_releases_on_shutdown(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "app.sqlite")
    seen = []

    def recovery():
        try:
            DatabaseLock(main.DB_PATH).acquire()
            seen.append("recovery ran WITHOUT the lock held")
        except DatabaseLockError:
            seen.append("recovery ran with the lock held")
        return []
    monkeypatch.setattr(main, "reconcile_interrupted", recovery)
    with TestClient(main.app) as client:
        assert seen == ["recovery ran with the lock held"]
        assert client.get("/api/health").status_code == 200
        with pytest.raises(DatabaseLockError):                      # still held for as long as the application runs
            DatabaseLock(main.DB_PATH).acquire()
    DatabaseLock(main.DB_PATH).acquire().release()                  # released at shutdown


def test_startup_is_refused_before_recovery_when_the_database_is_locked(tmp_path, monkeypatch, children):
    database = tmp_path / "app.sqlite"
    monkeypatch.setattr(main, "DB_PATH", database)
    ran = []
    monkeypatch.setattr(main, "reconcile_interrupted", lambda: ran.append(True) or [])
    hold(database, children)
    with pytest.raises(DatabaseLockError):
        with TestClient(main.app):
            pass
    assert ran == [] and not database.exists()                      # no recovery, and the database file was not even created


def test_an_unlockable_database_path_stops_application_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "missing-folder" / "app.sqlite")
    ran = []
    monkeypatch.setattr(main, "reconcile_interrupted", lambda: ran.append(True) or [])
    with pytest.raises(DatabaseLockError, match="does not exist"):
        with TestClient(main.app):
            pass
    assert ran == []


def test_importing_the_app_and_plain_test_clients_never_lock_anything(tmp_path):
    assert main.DB_PATH != BACKEND / "lazydesk.db"                  # conftest isolates the database
    before = sorted(p.name for p in main.DB_PATH.parent.iterdir())
    client = TestClient(main.app)                                   # no `with`: the lifespan (and so the lock) is not entered
    assert client.get("/api/health").status_code == 200
    assert sorted(p.name for p in main.DB_PATH.parent.iterdir()) == before      # no lock file appeared


def test_a_real_second_backend_is_refused_without_recovery_and_the_first_keeps_running(tmp_path, children):
    database = tmp_path / "shared.sqlite"
    first_port, second_port, third_port = free_port(), free_port(), free_port()
    first = backend(database, first_port, children)
    wait_until_serving(first, first_port)
    seed_interrupted_run(database)                                  # an interrupted run that ONLY a recovering backend would touch
    before = journal_snapshot(database)
    assert before[0][0][1] == "running"

    second = backend(database, second_port, children)
    output = second.communicate(timeout=WAIT)[0]
    assert second.returncode != 0
    assert "Only one backend may use a database at a time" in output and "LAZYDESK_DB" in output
    assert journal_snapshot(database) == before                     # the refused backend recovered nothing and wrote nothing
    with pytest.raises((urllib.error.URLError, OSError)):
        http_get(second_port, "/api/health")                        # it never served a request
    assert http_get(first_port, "/api/health")[0] == 200            # the owner is unaffected

    first.kill()                                                    # crash the owner: no cleanup runs
    first.communicate(timeout=WAIT)
    third = backend(database, third_port, children)                 # the OS released the lock; nothing to delete
    wait_until_serving(third, third_port)
    snapshot = journal_snapshot(database)
    assert snapshot[0][0][1] == "failed" and [row[2] for row in snapshot[1]] == ["skipped"]      # recovery ran, but only now
    third.kill()
    third.communicate(timeout=WAIT)
