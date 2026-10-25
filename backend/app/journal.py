"""Durable journal for Apply runs and Undo (Phase 4).

Every file move is described in the journal BEFORE it happens and updated right after, in SQLite with synchronous=FULL.
The journal records what LazyDesk intended and what it saw; after a crash, `reconcile` decides the truth from the file
system, and anything it cannot decide is marked `needs_attention` and left alone (it never moves a file itself).

Move states
  planned          recorded, not started
  moving           committed immediately before the rename; the rename may or may not have happened
  moved            the rename succeeded and was recorded
  failed           the move was attempted (or interrupted) and the file is still at its source
  skipped          never attempted because an earlier move failed or the run stopped
  undoing          committed immediately before the reverse rename
  undone           the file is back at its original path
  undo_blocked     Undo refused for this file; the file system was left untouched (reason in `detail`)
  needs_attention  the outcome is ambiguous; a person must look (reason in `detail`)

Run states (always derived from the move states by `derive_run_status`, never set optimistically)
  running · completed · partial · failed · needs_attention · undoing · undone · undo_partial · undo_blocked
"""
from __future__ import annotations

import os
import sqlite3
from typing import Callable, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS apply_runs (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS apply_moves (
    run_id TEXT NOT NULL REFERENCES apply_runs(id),
    seq INTEGER NOT NULL,
    action_id TEXT NOT NULL,
    source TEXT NOT NULL,
    destination TEXT NOT NULL,
    created_folder TEXT,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    fingerprint TEXT,
    state TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS plan_state (
    plan_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL
);
"""

MOVE_STATES = ("planned", "moving", "moved", "failed", "skipped", "undoing", "undone", "undo_blocked", "needs_attention")
RUN_STATES = ("running", "completed", "partial", "failed", "needs_attention", "undoing", "undone", "undo_partial", "undo_blocked")
IN_FLIGHT_MOVE = ("planned", "moving", "undoing")
UNDOABLE_RUNS = ("completed", "partial", "undo_partial", "undo_blocked", "failed")


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


def derive_run_status(states: Iterable[str], active: bool = False) -> str:
    """The one place a run's status is decided. `completed` requires EVERY move to be `moved`."""
    states = list(states)
    if "needs_attention" in states:
        return "needs_attention"
    if "undoing" in states:
        return "undoing"
    if active or "moving" in states:
        return "running"
    moved, undone, blocked = states.count("moved"), states.count("undone"), states.count("undo_blocked")
    if undone or blocked:
        remaining = moved + blocked
        if remaining == 0:
            return "undone"
        return "undo_partial" if undone else "undo_blocked"
    if states and moved == len(states):
        return "completed"
    if "planned" in states:  # a planned move that nobody finished: the run did not complete
        return "partial" if moved else "failed"
    return "partial" if moved else "failed"


def is_regular_file(path: str) -> bool:
    """A regular file that is not a symlink or Windows junction/reparse point."""
    try:
        if os.path.islink(path) or (hasattr(os.path, "isjunction") and os.path.isjunction(path)):
            return False
        return os.path.isfile(path)
    except OSError:
        return False


def matches_record(path: str, row: sqlite3.Row, fingerprint: Callable[[str], str | None]) -> bool:
    """Is `path` the file the journal recorded (size, mtime — which a rename preserves — and content hash when known)?"""
    if not is_regular_file(path):
        return False
    try:
        stat = os.stat(path)
        if stat.st_size != row["size"] or stat.st_mtime_ns != row["mtime_ns"]:
            return False
        return row["fingerprint"] is None or fingerprint(path) == row["fingerprint"]
    except OSError:
        return False


def new_run(connection: sqlite3.Connection, run_id: str, plan_id: str, workspace_root: str, plan_revision: int,
            moves: list[dict], now: str) -> None:
    """Record the whole run (every move as `planned`) in one committed transaction BEFORE any file is touched.
    Refuses if another run for the plan is still marked running."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        busy = connection.execute("SELECT id FROM apply_runs WHERE plan_id = ? AND status IN ('running', 'undoing')", (plan_id,)).fetchone()
        if busy:
            raise RunInProgress(busy["id"])
        connection.execute(
            "INSERT INTO apply_runs (id, plan_id, workspace_root, plan_revision, created_at, updated_at, status) VALUES (?, ?, ?, ?, ?, ?, 'running')",
            (run_id, plan_id, workspace_root, plan_revision, now, now))
        for seq, move in enumerate(moves):
            connection.execute(
                "INSERT INTO apply_moves (run_id, seq, action_id, source, destination, created_folder, size, mtime_ns, fingerprint, state, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?)",
                (run_id, seq, move["action_id"], move["source"], move["destination"], move.get("created_folder"),
                 move["size"], move["mtime_ns"], move.get("fingerprint"), now))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


class RunInProgress(Exception):
    def __init__(self, run_id: str):
        super().__init__(run_id)
        self.run_id = run_id


def set_move(connection: sqlite3.Connection, run_id: str, seq: int, state: str, now: str, detail: str = "",
             created_folder: str | None | bool = False, commit: bool = True) -> None:
    assert state in MOVE_STATES, state
    if created_folder is False:
        connection.execute("UPDATE apply_moves SET state = ?, detail = ?, updated_at = ? WHERE run_id = ? AND seq = ?", (state, detail, now, run_id, seq))
    else:
        connection.execute("UPDATE apply_moves SET state = ?, detail = ?, updated_at = ?, created_folder = ? WHERE run_id = ? AND seq = ?",
                           (state, detail, now, created_folder, run_id, seq))
    if commit:
        connection.commit()


def rows_for(connection: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM apply_moves WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()


def refresh_run(connection: sqlite3.Connection, run_id: str, now: str, active: bool = False, summary: str | None = None,
                commit: bool = True) -> str:
    status = derive_run_status([row["state"] for row in rows_for(connection, run_id)], active)
    finished = None if status in ("running", "undoing") else now
    if summary is None:
        connection.execute("UPDATE apply_runs SET status = ?, updated_at = ?, finished_at = ? WHERE id = ?", (status, now, finished, run_id))
    else:
        connection.execute("UPDATE apply_runs SET status = ?, updated_at = ?, finished_at = ?, summary = ? WHERE id = ?", (status, now, finished, summary, run_id))
    if commit:
        connection.commit()
    return status


# Test-only synchronization points ("before_read", "after_read"); empty in production.
RECONCILE_HOOKS: dict[str, Callable[[], None]] = {}


def _hook(name: str) -> None:
    hook = RECONCILE_HOOKS.get(name)
    if hook:
        hook()


def reconcile(connection: sqlite3.Connection, fingerprint: Callable[[str], str | None], now: str,
              is_active: Callable[[str], bool] = lambda run_id: False, on_moved: Callable[[str, str], None] | None = None,
              on_restored: Callable[[str, str], None] | None = None) -> list[dict]:
    """Settle runs that a crash or kill left in `running` / `undoing`, using the file system as the witness.

    Unambiguous outcomes are recorded; ambiguous ones become `needs_attention`. No file is ever moved, copied or deleted
    here (the only filesystem change is removing an EMPTY folder this run created before a move that never happened).

    `is_active(run_id)` says whether a run is being executed right now by this process. It is evaluated for each run AFTER
    the runs have been read: a run registers itself as active before its row is inserted, so any row seen here belongs to a
    run that is already registered, and a run that was inactive at that moment can no longer become active (a new run for
    the same plan is refused while a `running` row exists). A run that finishes between the read and the check is
    re-read below and left untouched."""
    report: list[dict] = []
    _hook("before_read")
    runs = connection.execute("SELECT * FROM apply_runs WHERE status IN ('running', 'undoing')").fetchall()
    _hook("after_read")
    for run in runs:
        if is_active(run["id"]):
            continue
        current = connection.execute("SELECT status FROM apply_runs WHERE id = ?", (run["id"],)).fetchone()
        if current is None or current["status"] not in ("running", "undoing"):
            continue  # finished (or settled elsewhere) since it was read
        changed = False
        for row in rows_for(connection, run["id"]):
            state = row["state"]
            if state not in IN_FLIGHT_MOVE:
                continue
            seq = row["seq"]
            new_state, detail = settle_row(row, fingerprint)
            if new_state == "failed":
                remove_empty_folder(row["created_folder"])
            set_move(connection, run["id"], seq, new_state, now, detail, commit=False)
            changed = True
            report.append({"run_id": run["id"], "seq": seq, "from": state, "to": new_state, "detail": detail})
            if new_state == "moved" and on_moved:
                on_moved(run["plan_id"], row["action_id"])
            if new_state == "undone" and on_restored:
                on_restored(run["plan_id"], row["action_id"])
        if changed:
            refresh_run(connection, run["id"], now, summary="Settled after an interruption; see the individual moves.", commit=False)
        connection.commit()
    return report


def settle_row(row: sqlite3.Row, fingerprint: Callable[[str], str | None]) -> tuple[str, str]:
    """What the file system says happened to an in-flight move: (new_state, explanation)."""
    state, source, destination = row["state"], row["source"], row["destination"]
    at_source = matches_record(source, row, fingerprint)
    at_destination = matches_record(destination, row, fingerprint)
    source_present, destination_present = os.path.lexists(source), os.path.lexists(destination)
    if state == "planned":
        return "skipped", "Not started when LazyDesk stopped."
    if state == "moving":
        if at_destination and not source_present:
            return "moved", "Recovered after an interruption: the file is at its destination."
        if at_source and not destination_present:
            return "failed", "Interrupted before the move happened; the file is still at its original location."
    elif state == "undoing":
        if at_source and not destination_present:
            return "undone", "Recovered after an interruption: the file is back at its original location."
        if at_destination and not source_present:
            return "moved", "Undo was interrupted before the file moved back; it is still at the destination."
    return "needs_attention", _ambiguity(source_present, destination_present, at_source, at_destination)


def _ambiguity(source_present: bool, destination_present: bool, at_source: bool, at_destination: bool) -> str:
    if source_present and destination_present:
        return "Both the original and the destination path exist; LazyDesk cannot tell which is correct. Nothing was changed. Please check both."
    if not source_present and not destination_present:
        return "The file is at neither the original nor the destination path. Nothing was changed. Please look for it."
    return "A file is present but it does not match what was recorded (it changed, or is not a regular file). Nothing was changed. Please check it."


def remove_empty_folder(folder: str | None) -> None:
    if not folder:
        return
    try:
        if os.path.isdir(folder) and not os.path.islink(folder):
            os.rmdir(folder)  # never recursive: raises if anything is inside
    except OSError:
        pass
