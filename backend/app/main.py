from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError, field_validator

APP_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("LAZYDESK_DB", APP_ROOT / "lazydesk.db"))
CACHE_SCHEMA_VERSION = 1
PROCESSING_VERSION = "rules-v1"
MAX_SCAN_ENTRIES = 10_000
MAX_CLASSIFICATION_BATCH = 20
SAFE_NAME = re.compile(r"^[^/\\\x00-\x1f\x7f]+$")
ALLOWED_CATEGORIES = {"documents", "images", "audio", "video", "archives", "code", "other"}
EASY_EXTENSIONS = {
    "documents": {"txt", "md", "pdf", "doc", "docx", "rtf", "odt", "csv", "xls", "xlsx"},
    "images": {"jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "tif", "tiff"},
    "audio": {"mp3", "wav", "flac", "aac", "ogg", "m4a"},
    "video": {"mp4", "mov", "mkv", "avi", "webm", "m4v"},
    "archives": {"zip", "tar", "gz", "rar", "7z", "bz2"},
    "code": {"py", "ts", "tsx", "js", "jsx", "css", "html", "json", "yaml", "yml", "go", "rs", "java"},
}

app = FastAPI(
    title="LazyDesk AI Studio API",
    version="0.2.0",
    description="Local-only file discovery, incremental classification, review, and safe organization APIs.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type"],
)


class ScanRequest(BaseModel):
    path: str = Field(min_length=1)
    use_ai: bool = True


class PlanRequest(BaseModel):
    files: list[str] = Field(default_factory=list, max_length=500)


class PlanAction(BaseModel):
    id: str
    source: str
    proposed_name: str
    current_location: str
    proposed_destination: str
    source_type: Literal["local-rule", "ollama", "cache"]
    validation: Literal["safe", "warning", "blocked"]
    reason: str
    status: Literal["pending", "approved", "rejected", "executed"] = "pending"
    workspace_root: str = ""
    source_size: int = -1
    source_mtime_ns: int = -1
    source_fingerprint: str | None = None


class PlanResponse(BaseModel):
    id: str
    created_at: str
    actions: list[PlanAction]


class ApprovalRequest(BaseModel):
    action_ids: list[str] = Field(default_factory=list)


class EditActionRequest(BaseModel):
    proposed_name: str = Field(min_length=1, max_length=255)


class ExecuteRequest(BaseModel):
    plan_id: str
    action_ids: list[str] = Field(default_factory=list)


class BenchmarkRequest(BaseModel):
    path: str | None = None
    synthetic_files: int = Field(default=0, ge=0, le=2000)


class BenchmarkResponse(BaseModel):
    id: str
    created_at: str
    path: str
    mode: Literal["workspace", "synthetic"]
    cold: dict[str, Any]
    warm: dict[str, Any]
    baseline_duration_ms: int
    optimized_duration_ms: int
    files_discovered: int
    unchanged_files_skipped: int
    cache_hits: int
    cache_misses: int
    local_model_requests: int
    throughput: float
    classification_quality: str


class ModelSettings(BaseModel):
    model: str | None = None


class OllamaClassification(BaseModel):
    category: Literal["documents", "images", "audio", "video", "archives", "code", "other"]
    proposed_name: str
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS benchmarks (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            actions TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS processing_cache (
            workspace_root TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            content_fingerprint TEXT,
            classification TEXT NOT NULL,
            classification_source TEXT NOT NULL,
            processing_status TEXT NOT NULL,
            cache_schema_version INTEGER NOT NULL,
            processing_version TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (workspace_root, relative_path)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    connection.commit()
    return connection


def record_activity(kind: str, message: str) -> None:
    connection = db()
    connection.execute(
        "INSERT INTO activity (kind, message, created_at) VALUES (?, ?, ?)",
        (kind, message, utc_now()),
    )
    connection.commit()
    connection.close()


def resolved_directory(raw_path: str) -> Path:
    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Directory does not exist.")
    if not path.is_dir():
        raise HTTPException(status_code=400, detail="The selected path is not a directory.")
    return path


def root_relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Path is outside the selected workspace.") from error


def is_symlink_safe(path: Path, root: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def content_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def suffix_category(path: Path) -> str | None:
    extension = path.suffix.lower().lstrip(".")
    for category, extensions in EASY_EXTENSIONS.items():
        if extension in extensions:
            return category
    return None


def file_item(path: Path, root: Path, classification: dict[str, Any] | None = None) -> dict[str, Any]:
    stat = path.stat()
    result: dict[str, Any] = {
        "id": hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12],
        "name": path.name,
        "path": str(path),
        "relative_path": root_relative(path, root),
        "extension": path.suffix.lower().lstrip(".") or "file",
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "is_directory": False,
    }
    if classification:
        result.update(classification)
    return result


def scan_files(root: Path) -> list[Path]:
    items: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item).lower()):
        if len(items) >= MAX_SCAN_ENTRIES:
            break
        if path.resolve() == DB_PATH.resolve():
            continue
        if path.is_file() and is_symlink_safe(path, root):
            items.append(path)
    return items


def deterministic_classification(path: Path) -> tuple[str, str]:
    category = suffix_category(path) or "other"
    return category, f"Matched the .{path.suffix.lower().lstrip('.') or 'file'} extension using a deterministic local rule."


def ollama_settings() -> tuple[str, str | None]:
    model = os.getenv("OLLAMA_MODEL")
    try:
        connection = db()
        row = connection.execute("SELECT value FROM settings WHERE key = 'ollama_model'").fetchone()
        connection.close()
        if row and row["value"]:
            model = row["value"]
    except sqlite3.Error:
        pass
    return os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/"), model


def ollama_models() -> list[str]:
    host, _ = ollama_settings()
    try:
        request = urllib.request.Request(f"{host}/api/tags", method="GET")
        with urllib.request.urlopen(request, timeout=0.8) as response:
            payload = json.loads(response.read())
        return [item["name"] for item in payload.get("models", []) if isinstance(item, dict) and item.get("name")]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def classify_with_ollama(path: Path, model: str) -> OllamaClassification:
    host, _ = ollama_settings()
    prompt = (
        "Classify this file using only metadata. Return JSON with category, proposed_name, and reason. "
        f"Allowed categories: {', '.join(sorted(ALLOWED_CATEGORIES))}. "
        f"Filename: {path.name}; extension: {path.suffix.lower()}; size_bytes: {path.stat().st_size}."
    )
    request = urllib.request.Request(
        f"{host}/api/generate",
        data=json.dumps({"model": model, "prompt": prompt, "format": "json", "stream": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=4.0) as response:
        payload = json.loads(response.read())
    raw = payload.get("response")
    if not isinstance(raw, str):
        raise ValueError("Ollama returned no structured response.")
    parsed = OllamaClassification.model_validate(json.loads(raw))
    if not SAFE_NAME.fullmatch(parsed.proposed_name) or Path(parsed.proposed_name).name != parsed.proposed_name:
        raise ValueError("Ollama proposed an unsafe filename.")
    if Path(parsed.proposed_name).suffix.lower() != path.suffix.lower():
        parsed.proposed_name = path.name
    return parsed


def classify_file(path: Path, use_ai: bool) -> tuple[str, str, str, str | None, bool]:
    easy = suffix_category(path)
    if easy:
        return easy, "local-rule", "Deterministic extension rule avoided an inference request.", None, False
    model = ollama_settings()[1]
    if use_ai and model and model in ollama_models():
        try:
            result = classify_with_ollama(path, model)
            return result.category, "ollama", result.reason, result.proposed_name, True
        except (OSError, ValueError, ValidationError, json.JSONDecodeError, urllib.error.URLError):
            category, reason = deterministic_classification(path)
            return category, "local-rule", f"Ollama response was unavailable or invalid; {reason}", None, True
    category, reason = deterministic_classification(path)
    return category, "local-rule", reason, None, False


def classify_scan(root: Path, files: list[Path], use_ai: bool) -> tuple[list[dict[str, Any]], dict[str, int]]:
    connection = db()
    rows = {
        row["relative_path"]: row
        for row in connection.execute(
            "SELECT * FROM processing_cache WHERE workspace_root = ?", (str(root),)
        ).fetchall()
    }
    current_paths = {root_relative(path, root) for path in files}
    connection.execute("DELETE FROM processing_cache WHERE workspace_root = ? AND relative_path NOT IN ({})".format(
        ",".join("?" for _ in current_paths) or "''"
    ), (str(root), *current_paths))
    results: list[dict[str, Any]] = []
    stats = {"cache_hits": 0, "cache_misses": 0, "model_requests": 0, "reprocessed": 0}
    for path in files:
        relative = root_relative(path, root)
        stat = path.stat()
        cached = rows.get(relative)
        category: str
        source: str
        reason: str
        proposed_name: str | None
        reusable = (
            cached is not None
            and cached["cache_schema_version"] == CACHE_SCHEMA_VERSION
            and cached["processing_version"] == PROCESSING_VERSION
            and cached["processing_status"] == "complete"
            and cached["file_size"] == stat.st_size
            and cached["mtime_ns"] == stat.st_mtime_ns
        )
        if cached and cached["content_fingerprint"]:
            reusable = reusable and cached["content_fingerprint"] == content_fingerprint(path)
        if reusable:
            category, source, reason, proposed_name = (
                cached["classification"], f"cache:{cached['classification_source']}", "Reused a valid cached classification.", None
            )
            stats["cache_hits"] += 1
        else:
            stats["cache_misses"] += 1
            if cached:
                stats["reprocessed"] += 1
            connection.execute(
                """
                INSERT INTO processing_cache VALUES (?, ?, ?, ?, NULL, ?, ?, 'processing', ?, ?, ?)
                ON CONFLICT(workspace_root, relative_path) DO UPDATE SET
                file_size=excluded.file_size, mtime_ns=excluded.mtime_ns,
                processing_status='processing', updated_at=excluded.updated_at
                """,
                (str(root), relative, stat.st_size, stat.st_mtime_ns, "pending", "pending",
                 CACHE_SCHEMA_VERSION, PROCESSING_VERSION, utc_now()),
            )
            category, source, reason, proposed_name, attempted_model = classify_file(path, use_ai)
            if attempted_model:
                stats["model_requests"] += 1
            fingerprint = content_fingerprint(path) if suffix_category(path) is None else None
            connection.execute(
                """
                UPDATE processing_cache SET content_fingerprint=?, classification=?, classification_source=?,
                processing_status='complete', updated_at=? WHERE workspace_root=? AND relative_path=?
                """,
                (fingerprint, category, source, utc_now(), str(root), relative),
            )
        results.append(file_item(path, root, {
            "category": category, "classification_source": source, "classification_reason": reason,
            "proposed_name": proposed_name or path.name,
        }))
    connection.commit()
    connection.close()
    return results, stats


def validate_action(source: Path, destination: Path, root: Path) -> tuple[str, str]:
    if not is_symlink_safe(source, root) or destination.is_symlink():
        return "blocked", "Symbolic links are not accepted for file operations."
    if not SAFE_NAME.fullmatch(destination.name):
        return "blocked", "The proposed filename contains path separators or control characters."
    root = root.resolve()
    destination = destination.resolve()
    try:
        source.resolve().relative_to(root)
        destination.relative_to(root)
    except ValueError:
        return "blocked", "The operation must stay inside the selected workspace."
    if source == destination:
        return "blocked", "The proposed destination is unchanged."
    if destination.exists():
        return "blocked", "A file already exists at the proposed destination."
    if source.parent != destination.parent:
        return "warning", "This operation moves the file into a new folder."
    return "safe", "Destination is inside the workspace and currently unused."


def action_for(source: Path, root: Path, proposed_name: str | None = None) -> PlanAction:
    name = proposed_name or source.name
    category_dir = root / (suffix_category(source) or "other")
    destination = category_dir / name
    if not SAFE_NAME.fullmatch(name):
        validation, reason = "blocked", "The proposed filename contains path separators or control characters."
    else:
        validation, reason = validate_action(source, destination, root)
    stat = source.stat()
    fingerprint = content_fingerprint(source) if suffix_category(source) is None else None
    action_id = hashlib.sha256(f"{root}:{source}:{destination}".encode()).hexdigest()[:16]
    return PlanAction(
        id=action_id, source=str(source), proposed_name=name, current_location=str(source.parent),
        proposed_destination=str(destination), source_type="local-rule", validation=validation,
        reason=reason, workspace_root=str(root), source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns,
        source_fingerprint=fingerprint,
    )


@app.get("/api/health")
def health() -> dict[str, Any]:
    host, model = ollama_settings()
    models = ollama_models()
    return {"status": "ok", "ollama": {"available": bool(models), "url": host, "models": models, "selected_model": model}}


@app.get("/api/settings", response_model=ModelSettings)
def settings() -> ModelSettings:
    return ModelSettings(model=ollama_settings()[1])


@app.patch("/api/settings", response_model=ModelSettings)
def update_settings(request: ModelSettings) -> ModelSettings:
    models = ollama_models()
    if request.model is not None and request.model not in models:
        raise HTTPException(status_code=400, detail="Selected Ollama model is not installed locally.")
    connection = db()
    connection.execute(
        "INSERT INTO settings(key, value) VALUES ('ollama_model', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (request.model,),
    )
    connection.commit()
    connection.close()
    record_activity("settings", f"Selected Ollama model: {request.model or 'none'}")
    return ModelSettings(model=request.model)


@app.post("/api/scan")
def scan(request: ScanRequest) -> dict[str, Any]:
    root = resolved_directory(request.path)
    started = time.perf_counter()
    items, stats = classify_scan(root, scan_files(root), request.use_ai)
    duration_ms = max(1, round((time.perf_counter() - started) * 1000))
    record_activity("scan", f"Scanned {root} ({len(items)} files, {stats['cache_hits']} cache hits)")
    return {"path": str(root), "items": items, "count": len(items), "processing": {**stats, "duration_ms": duration_ms}}


@app.post("/api/plans", response_model=PlanResponse)
def plans(request: PlanRequest) -> PlanResponse:
    if not request.files:
        raise HTTPException(status_code=400, detail="Select at least one file to create a plan.")
    unresolved = [Path(raw).expanduser() for raw in request.files]
    paths = [path.resolve() for path in unresolved]
    roots = {path.parent for path in paths}
    if len(roots) != 1 or any(not path.is_file() or path.is_symlink() for path in paths) or any(path.is_symlink() for path in unresolved):
        raise HTTPException(status_code=400, detail="Files must exist, be regular files, and share one parent directory.")
    actions = [action_for(path, next(iter(roots))) for path in paths]
    plan = PlanResponse(id=hashlib.sha256(f"{utc_now()}:{paths}".encode()).hexdigest()[:16], created_at=utc_now(), actions=actions)
    connection = db()
    connection.execute("INSERT INTO plans VALUES (?, ?, ?)", (plan.id, plan.created_at, json.dumps([item.model_dump() for item in actions])))
    connection.commit()
    connection.close()
    record_activity("plan", f"Created review plan with {len(actions)} proposals")
    return plan


def load_plan(connection: sqlite3.Connection, plan_id: str) -> tuple[sqlite3.Row, list[PlanAction]]:
    row = connection.execute("SELECT id, created_at, actions FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Review plan was not found.")
    try:
        return row, [PlanAction(**item) for item in json.loads(row["actions"])]
    except (json.JSONDecodeError, ValidationError) as error:
        raise HTTPException(status_code=500, detail="Stored review plan is invalid.") from error


def save_plan(connection: sqlite3.Connection, plan_id: str, actions: list[PlanAction]) -> None:
    connection.execute("UPDATE plans SET actions = ? WHERE id = ?", (json.dumps([item.model_dump() for item in actions]), plan_id))
    connection.commit()


@app.patch("/api/plans/{plan_id}/actions/{action_id}", response_model=PlanResponse)
def edit_action(plan_id: str, action_id: str, request: EditActionRequest) -> PlanResponse:
    connection = db()
    row, actions = load_plan(connection, plan_id)
    action = next((item for item in actions if item.id == action_id), None)
    if action is None:
        connection.close()
        raise HTTPException(status_code=404, detail="Plan action was not found.")
    if action.status == "executed":
        connection.close()
        raise HTTPException(status_code=409, detail="This action has already been executed and cannot be edited.")
    source = Path(action.source)
    if not source.is_file() or source.is_symlink():
        action.validation, action.reason = "blocked", "The source file is stale or no longer a regular file."
    else:
        updated = action_for(source, Path(action.workspace_root), request.proposed_name)
        action.proposed_name, action.proposed_destination = updated.proposed_name, updated.proposed_destination
        action.validation, action.reason = updated.validation, updated.reason
    # Any edit voids a previous approval; a rejection is never silently reversed.
    if action.status == "approved":
        action.status = "pending"
    save_plan(connection, plan_id, actions)
    connection.close()
    return PlanResponse(id=row["id"], created_at=row["created_at"], actions=actions)


@app.post("/api/plans/{plan_id}/approve", response_model=PlanResponse)
def approve(plan_id: str, request: ApprovalRequest) -> PlanResponse:
    connection = db()
    row, actions = load_plan(connection, plan_id)
    selected = set(request.action_ids)
    known = {item.id for item in actions}
    if not selected.issubset(known):
        connection.close()
        raise HTTPException(status_code=400, detail="Approval contains an action from another plan.")
    for action in actions:
        if action.id in selected:
            if action.status in ("rejected", "executed"):
                connection.close()
                raise HTTPException(status_code=409, detail=f"Action {action.id} is {action.status} and cannot be approved.")
            if action.validation == "blocked":
                connection.close()
                raise HTTPException(status_code=409, detail=f"Action {action.id} is blocked and cannot be approved.")
            problem = execution_problem(action)
            if problem:
                connection.close()
                raise HTTPException(status_code=409, detail=f"Action {action.id} cannot be approved: {problem}")
    for action in actions:
        if action.id in selected:
            action.status = "approved"
    save_plan(connection, plan_id, actions)
    connection.close()
    record_activity("approval", f"Approved {len(selected)} proposal(s) in plan {plan_id}")
    return PlanResponse(id=row["id"], created_at=row["created_at"], actions=actions)


@app.post("/api/plans/{plan_id}/reject", response_model=PlanResponse)
def reject(plan_id: str, request: ApprovalRequest) -> PlanResponse:
    connection = db()
    row, actions = load_plan(connection, plan_id)
    selected = set(request.action_ids)
    if not selected.issubset({item.id for item in actions}):
        connection.close()
        raise HTTPException(status_code=400, detail="Rejection contains an action from another plan.")
    for action in actions:
        if action.id in selected:
            if action.status == "executed":
                connection.close()
                raise HTTPException(status_code=409, detail=f"Action {action.id} has already been executed.")
            action.status = "rejected"
    save_plan(connection, plan_id, actions)
    connection.close()
    record_activity("rejection", f"Rejected {len(selected)} proposal(s) in plan {plan_id}")
    return PlanResponse(id=row["id"], created_at=row["created_at"], actions=actions)


def execution_problem(action: PlanAction) -> str | None:
    """Re-check an action against the live filesystem. Returns a reason it must not run, or None."""
    root, source, destination = Path(action.workspace_root), Path(action.source), Path(action.proposed_destination)
    try:
        if not action.workspace_root or not root.is_dir() or root.is_symlink():
            return "the workspace is missing or unsafe"
        resolved_root = root.resolve()
        if not SAFE_NAME.fullmatch(destination.name):
            return "the destination filename is invalid"
        if not source.is_file() or not is_symlink_safe(source, resolved_root):
            return "the source is missing, not a regular file, or unsafe"
        try:
            destination.resolve().relative_to(resolved_root)
        except ValueError:
            return "the destination is outside the workspace"
        if destination.is_symlink() or destination.exists():
            return "the destination is occupied"
        if source.resolve() == destination.resolve():
            return "the destination is unchanged"
        current = source.stat()
        if current.st_size != action.source_size or current.st_mtime_ns != action.source_mtime_ns:
            return "the source file changed since the plan was created"
        if action.source_fingerprint and content_fingerprint(source) != action.source_fingerprint:
            return "the source content changed since the plan was created"
    except OSError as error:
        return f"the filesystem could not be checked ({error})"
    return None


def move_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not is_symlink_safe(destination.parent, destination.parent):
        raise OSError("destination folder is a symbolic link")
    source.rename(destination)


@app.post("/api/execute")
def execute(request: ExecuteRequest) -> dict[str, Any]:
    if not request.action_ids:
        raise HTTPException(status_code=400, detail="Select at least one approved action.")
    action_ids = list(dict.fromkeys(request.action_ids))
    connection = db()
    _, actions = load_plan(connection, request.plan_id)
    action_map = {action.id: action for action in actions}
    if not set(action_ids).issubset(action_map):
        connection.close()
        raise HTTPException(status_code=400, detail="Execution contains an action from another plan.")
    # Preflight every action before touching anything, so a bad batch changes nothing.
    destinations: set[str] = set()
    for action_id in action_ids:
        action = action_map[action_id]
        problem = None
        if action.status != "approved":
            problem = f"it is {action.status}, not approved"
        elif action.validation == "blocked":
            problem = "it is blocked"
        elif action.proposed_destination in destinations:
            problem = "another action in this batch targets the same destination"
        else:
            problem = execution_problem(action)
        if problem:
            connection.close()
            raise HTTPException(status_code=409, detail=f"Action {action_id} was not executed: {problem}.")
        destinations.add(action.proposed_destination)
    # Moves are performed one by one and are NOT atomic or reversible: on a failure the
    # earlier moves stay applied and are reported, and the remaining ones are not attempted.
    moved: list[str] = []
    failure: str | None = None
    for action_id in action_ids:
        action = action_map[action_id]
        try:
            move_file(Path(action.source), Path(action.proposed_destination))
        except OSError as error:
            failure = f"Operation {action_id} failed: {error}"
            break
        action.status = "executed"
        moved.append(action_id)
    save_plan(connection, request.plan_id, actions)
    connection.close()
    record_activity("execution", f"Executed {len(moved)} of {len(action_ids)} approved file operation(s) from plan {request.plan_id}")
    if failure:
        raise HTTPException(status_code=500, detail={"message": failure, "moved": moved, "not_attempted": [i for i in action_ids if i not in moved and i != action_id]})
    return {"moved": moved, "count": len(moved), "plan_id": request.plan_id}


@app.post("/api/duplicates")
def duplicates(request: ScanRequest) -> dict[str, Any]:
    root = resolved_directory(request.path)
    groups: dict[tuple[int, str], list[str]] = {}
    for path in scan_files(root):
        try:
            fingerprint = content_fingerprint(path)
            groups.setdefault((path.stat().st_size, fingerprint), []).append(str(path))
        except OSError:
            continue
    matches = [{"size": size, "fingerprint": fingerprint, "files": files} for (size, fingerprint), files in groups.items() if len(files) > 1]
    record_activity("duplicates", f"Found {len(matches)} exact duplicate group(s) in {root}")
    return {"path": str(root), "groups": matches, "count": len(matches), "fingerprint": "SHA-256 content hash"}


def benchmark_once(root: Path, use_cache: bool) -> dict[str, Any]:
    started = time.perf_counter()
    files = scan_files(root)
    if use_cache:
        _, stats = classify_scan(root, files, use_ai=False)
    else:
        stats = {"cache_hits": 0, "cache_misses": len(files), "model_requests": 0, "reprocessed": 0}
        for path in files:
            deterministic_classification(path)
    duration = max(1, round((time.perf_counter() - started) * 1000))
    return {"files": len(files), "processed": stats["cache_misses"], "cache_hits": stats["cache_hits"], "duration_ms": duration}


@app.post("/api/benchmarks", response_model=BenchmarkResponse)
def benchmark(request: BenchmarkRequest) -> BenchmarkResponse:
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if request.synthetic_files:
        temporary = tempfile.TemporaryDirectory(prefix="lazydesk-benchmark-")
        root = Path(temporary.name)
        for index in range(request.synthetic_files):
            (root / f"sample-{index:04d}.txt").write_text(f"deterministic sample {index}\n", encoding="utf-8")
    elif request.path:
        root = resolved_directory(request.path)
    else:
        raise HTTPException(status_code=400, detail="Provide a workspace path or synthetic_files count.")
    cold = benchmark_once(root, use_cache=False)
    optimized_cold = benchmark_once(root, use_cache=True)
    warm = benchmark_once(root, use_cache=True)
    warm["optimized_cold_duration_ms"] = optimized_cold["duration_ms"]
    payload = BenchmarkResponse(
        id=hashlib.sha256(f"{root}:{utc_now()}".encode()).hexdigest()[:16], created_at=utc_now(),
        path=str(root), mode="synthetic" if temporary else "workspace", cold=cold, warm=warm,
        baseline_duration_ms=cold["duration_ms"], optimized_duration_ms=warm["duration_ms"],
        files_discovered=warm["files"], unchanged_files_skipped=warm["cache_hits"],
        cache_hits=warm["cache_hits"], cache_misses=warm["processed"], local_model_requests=0,
        throughput=round(warm["files"] / (warm["duration_ms"] / 1000), 2) if warm["duration_ms"] else 0,
        classification_quality="Deterministic categories; no labeled accuracy set supplied.",
    )
    connection = db()
    connection.execute("INSERT INTO benchmarks VALUES (?, ?, ?)", (payload.id, payload.created_at, payload.model_dump_json()))
    connection.commit()
    connection.close()
    if temporary:
        temporary.cleanup()
    record_activity("benchmark", f"Benchmarked {root} ({warm['files']} files, cold and warm)")
    return payload


@app.get("/api/activity")
def activity() -> dict[str, Any]:
    connection = db()
    rows = connection.execute("SELECT id, kind, message, created_at FROM activity ORDER BY id DESC LIMIT 50").fetchall()
    connection.close()
    return {"items": [dict(row) for row in rows]}


@app.get("/api/benchmarks")
def benchmark_history() -> dict[str, Any]:
    connection = db()
    rows = connection.execute("SELECT payload FROM benchmarks ORDER BY created_at DESC LIMIT 20").fetchall()
    connection.close()
    return {"items": [json.loads(row["payload"]) for row in rows]}
