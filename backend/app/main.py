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
PROCESSING_VERSION = "rules-v2"
MAX_SCAN_ENTRIES = 10_000
MAX_CLASSIFICATION_BATCH = 20  # model requests attempted per scan; the rest wait for the next scan
MAX_CONSECUTIVE_MODEL_FAILURES = 2  # stop calling an unresponsive model for the rest of a scan
FINGERPRINT_LIMIT_BYTES = 64 * 1024 * 1024  # larger files are identified by size and mtime only
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("LAZYDESK_OLLAMA_TIMEOUT", "20"))
SAFE_NAME = re.compile(r"^[^/\\\x00-\x1f\x7f]+$")
WINDOWS_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
# Windows file attributes that mean the content lives in the cloud and reading it would download it.
CLOUD_ONLY_ATTRIBUTES = 0x1000 | 0x40000 | 0x400000  # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS
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
    workspace_root: str = Field(min_length=1)
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
    category: str = ""


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
    repetitions: int = Field(default=3, ge=1, le=9)


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
    optimized_cold: dict[str, Any] = Field(default_factory=dict)
    samples: dict[str, list[float]] = Field(default_factory=dict)
    discovery_ms: float = 0
    results_match: bool = True
    speedup_warm_vs_baseline: float = 0
    methodology: str = ""


class ModelSettings(BaseModel):
    model: str | None = None


class OllamaClassification(BaseModel):
    category: Literal["documents", "images", "audio", "video", "archives", "code", "other"]
    proposed_name: str
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("proposed_name", mode="before")
    @classmethod
    def strip_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("reason", mode="before")
    @classmethod
    def trim_reason(cls, value: Any) -> Any:
        return value.strip()[:500] if isinstance(value, str) else value


def unsafe_name_reason(name: str) -> str | None:
    """Why `name` must not be used as a single file name, or None when it is acceptable."""
    if not name or name in {".", ".."}:
        return "The proposed filename is empty or a relative path component."
    if not SAFE_NAME.fullmatch(name):
        return "The proposed filename contains path separators or control characters."
    if len(name) > 255:
        return "The proposed filename is longer than 255 characters."
    if name != name.strip() or name.endswith("."):
        return "The proposed filename has leading or trailing spaces or a trailing dot."
    if any(char in name for char in '<>:"|?*'):
        return "The proposed filename contains characters that Windows does not allow."
    if name.split(".")[0].strip().lower() in WINDOWS_RESERVED_NAMES:
        return "The proposed filename is a reserved Windows device name."
    return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db(path: Path | None = None) -> sqlite3.Connection:
    connection = sqlite3.connect(path or DB_PATH, timeout=10)
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
            details TEXT,
            PRIMARY KEY (workspace_root, relative_path)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(processing_cache)")}
    if "details" not in columns:  # databases created before model suggestions were persisted
        connection.execute("ALTER TABLE processing_cache ADD COLUMN details TEXT")
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


def is_cloud_placeholder(path: Path) -> bool:
    """True for OneDrive-style files whose content is not local; hashing them would download them."""
    try:
        return bool(getattr(path.stat(), "st_file_attributes", 0) & CLOUD_ONLY_ATTRIBUTES)
    except OSError:
        return False


def identity_fingerprint(path: Path) -> str | None:
    """SHA-256 used to detect a changed source, or None when hashing is too costly or would download the file."""
    try:
        if path.stat().st_size > FINGERPRINT_LIMIT_BYTES or is_cloud_placeholder(path):
            return None
        return content_fingerprint(path)
    except OSError:
        return None


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


def scan_directory(root: Path) -> tuple[list[Path], bool]:
    """Return eligible files (capped at MAX_SCAN_ENTRIES) and whether more eligible files were left unscanned."""
    items: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item).lower()):
        if path.resolve() == DB_PATH.resolve():
            continue
        if path.is_file() and is_symlink_safe(path, root):
            if len(items) >= MAX_SCAN_ENTRIES:
                return items, True
            items.append(path)
    return items, False


def scan_files(root: Path) -> list[Path]:
    return scan_directory(root)[0]


def completeness(root: Path, scanned: int, truncated: bool) -> dict[str, Any]:
    return {
        "scan_root": str(root),
        "files_scanned": scanned,
        "limit": MAX_SCAN_ENTRIES,
        "truncated": truncated,
        "complete": not truncated,
    }


def resolve_plan_sources(root: Path, raw_files: list[str]) -> list[Path]:
    """Resolve selected files, requiring each to be a regular, non-symlinked file inside the selected root."""
    if root == root.parent:
        raise HTTPException(status_code=400, detail="A filesystem root cannot be used as a workspace.")
    sources: list[Path] = []
    for raw in raw_files:
        candidate = Path(raw).expanduser()
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=f"{raw} is outside the selected workspace.") from error
        if candidate.is_symlink() or resolved.is_symlink() or not resolved.is_file():
            raise HTTPException(status_code=400, detail=f"{raw} must be an existing regular file (symbolic links are not accepted).")
        sources.append(resolved)
    return list(dict.fromkeys(sources))


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
        "You classify files using ONLY their metadata. Respond with one JSON object and nothing else, "
        'with exactly these keys: "category", "proposed_name", "reason". '
        f"category must be one of: {', '.join(sorted(ALLOWED_CATEGORIES))}. "
        "proposed_name must be a single file name with no folders and the same extension as the original. "
        "reason is one short sentence. "
        f"Filename: {path.name}; extension: {path.suffix.lower() or 'none'}; size_bytes: {path.stat().st_size}."
    )
    request = urllib.request.Request(
        f"{host}/api/generate",
        data=json.dumps({
            "model": model, "prompt": prompt, "format": "json", "stream": False, "options": {"temperature": 0},
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read())
    raw = payload.get("response") if isinstance(payload, dict) else None
    if not isinstance(raw, str):
        raise ValueError("Ollama returned no structured response.")
    parsed = OllamaClassification.model_validate(json.loads(raw))
    problem = unsafe_name_reason(parsed.proposed_name)
    if problem or Path(parsed.proposed_name).name != parsed.proposed_name:
        raise ValueError(problem or "Ollama proposed a path instead of a file name.")
    if Path(parsed.proposed_name).suffix.lower() != path.suffix.lower():
        parsed.proposed_name = path.name  # never let the model change the file type
    return parsed


class Classification:
    """Outcome of classifying one file. `failed` means a model call was attempted and had to fall back."""

    def __init__(self, category: str, source: str, reason: str, proposed_name: str | None = None,
                 attempted_model: bool = False, failed: bool = False, model: str | None = None) -> None:
        self.category, self.source, self.reason, self.proposed_name = category, source, reason, proposed_name
        self.attempted_model, self.failed, self.model = attempted_model, failed, model


class ModelSession:
    """Per-scan view of the optional local model: resolves availability once and bounds the work done."""

    def __init__(self, use_ai: bool) -> None:
        self.use_ai = use_ai
        self._resolved = False
        self._model: str | None = None
        self.requests = 0
        self.consecutive_failures = 0

    @property
    def model(self) -> str | None:
        if not self._resolved:
            self._resolved = True
            selected = ollama_settings()[1] if self.use_ai else None
            if selected and selected in ollama_models():
                self._model = selected
        return self._model

    def exhausted_reason(self) -> str | None:
        if self.requests >= MAX_CLASSIFICATION_BATCH:
            return f"the {MAX_CLASSIFICATION_BATCH}-request limit for one scan was reached"
        if self.consecutive_failures >= MAX_CONSECUTIVE_MODEL_FAILURES:
            return "the model failed repeatedly during this scan"
        return None


def classify_file(path: Path, session: ModelSession) -> Classification:
    easy = suffix_category(path)
    if easy:
        return Classification(easy, "local-rule", "Deterministic extension rule avoided an inference request.")
    category, rule_reason = deterministic_classification(path)
    model = session.model
    if not model:
        return Classification(category, "local-rule", rule_reason)
    limit = session.exhausted_reason()
    if limit:
        return Classification(category, "local-rule", f"Model skipped because {limit}; {rule_reason}", failed=True)
    session.requests += 1
    try:
        result = classify_with_ollama(path, model)
    except (OSError, ValueError, ValidationError, urllib.error.URLError) as error:  # JSONDecodeError is a ValueError
        session.consecutive_failures += 1
        return Classification(
            category, "local-rule",
            f"Ollama response was unavailable or invalid ({type(error).__name__}); {rule_reason}",
            attempted_model=True, failed=True, model=model,
        )
    session.consecutive_failures = 0
    return Classification(result.category, "ollama", result.reason, result.proposed_name, attempted_model=True, model=model)


def parse_details(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def cached_row_is_current(cached: sqlite3.Row | None, path: Path, stat: os.stat_result) -> bool:
    """A cache row may be reused only if it was fully computed by this version for this exact file state."""
    if cached is None:
        return False
    if not (
        cached["cache_schema_version"] == CACHE_SCHEMA_VERSION
        and cached["processing_version"] == PROCESSING_VERSION
        and cached["processing_status"] == "complete"
        and cached["file_size"] == stat.st_size
        and cached["mtime_ns"] == stat.st_mtime_ns
    ):
        return False
    if cached["content_fingerprint"]:
        return cached["content_fingerprint"] == identity_fingerprint(path)
    return True


def classify_scan(
    root: Path, files: list[Path], use_ai: bool, connection: sqlite3.Connection | None = None
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    owns_connection = connection is None
    connection = connection or db()
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
    session = ModelSession(use_ai)
    results: list[dict[str, Any]] = []
    stats = {"cache_hits": 0, "cache_misses": 0, "model_requests": 0, "reprocessed": 0, "model_fallbacks": 0}
    for path in files:
        relative = root_relative(path, root)
        stat = path.stat()
        cached = rows.get(relative)
        ambiguous = suffix_category(path) is None
        reusable = cached_row_is_current(cached, path, stat)
        details = parse_details(cached["details"]) if cached is not None else {}
        if reusable and ambiguous and session.use_ai:
            # A rule-only or other-model answer is stale once a (different) model is available to improve it.
            current_model = session.model
            if current_model and (cached["classification_source"] != "ollama" or details.get("model") != current_model):
                reusable = False
        if reusable:
            stats["cache_hits"] += 1
            category, source = cached["classification"], f"cache:{cached['classification_source']}"
            reason = details.get("reason") or "Reused a valid cached classification."
            proposed_name = details.get("proposed_name")
        else:
            stats["cache_misses"] += 1
            if cached:
                stats["reprocessed"] += 1
            fingerprint = identity_fingerprint(path) if ambiguous else None
            connection.execute(
                """
                INSERT INTO processing_cache (workspace_root, relative_path, file_size, mtime_ns, content_fingerprint,
                    classification, classification_source, processing_status, cache_schema_version,
                    processing_version, updated_at, details)
                VALUES (?, ?, ?, ?, NULL, 'pending', 'pending', 'processing', ?, ?, ?, NULL)
                ON CONFLICT(workspace_root, relative_path) DO UPDATE SET
                file_size=excluded.file_size, mtime_ns=excluded.mtime_ns, content_fingerprint=NULL,
                processing_status='processing', details=NULL, updated_at=excluded.updated_at
                """,
                (str(root), relative, stat.st_size, stat.st_mtime_ns, CACHE_SCHEMA_VERSION, PROCESSING_VERSION, utc_now()),
            )
            result = classify_file(path, session)
            if result.attempted_model:
                stats["model_requests"] += 1
            if result.failed:
                stats["model_fallbacks"] += 1
            category, source, reason, proposed_name = result.category, result.source, result.reason, result.proposed_name
            stored_details = json.dumps({"model": result.model, "proposed_name": proposed_name, "reason": reason}) if source == "ollama" else None
            # A fallback after a failed or skipped model call is not final: leave it incomplete so the next scan retries.
            status = "fallback" if result.failed else "complete"
            connection.execute(
                """
                UPDATE processing_cache SET content_fingerprint=?, classification=?, classification_source=?,
                processing_status=?, details=?, updated_at=? WHERE workspace_root=? AND relative_path=?
                """,
                (fingerprint, category, source, status, stored_details, utc_now(), str(root), relative),
            )
        results.append(file_item(path, root, {
            "category": category, "classification_source": source, "classification_reason": reason,
            "proposed_name": proposed_name or path.name,
        }))
    connection.commit()
    if owns_connection:
        connection.close()
    return results, stats


def classify_without_cache(root: Path, files: list[Path]) -> list[dict[str, Any]]:
    """The no-cache baseline: same inputs and output shape as classify_scan, deterministic rules only."""
    items = []
    for path in files:
        category, reason = deterministic_classification(path)
        items.append(file_item(path, root, {
            "category": category, "classification_source": "local-rule", "classification_reason": reason,
            "proposed_name": path.name,
        }))
    return items


def plan_suggestion(connection: sqlite3.Connection, root: Path, path: Path) -> dict[str, Any] | None:
    """A still-valid, safety-checked model suggestion cached for this file, or None to use the deterministic rule."""
    try:
        cached = connection.execute(
            "SELECT * FROM processing_cache WHERE workspace_root = ? AND relative_path = ?",
            (str(root), root_relative(path, root)),
        ).fetchone()
        if cached is None or cached["classification_source"] != "ollama" or not cached_row_is_current(cached, path, path.stat()):
            return None
    except (OSError, sqlite3.Error, HTTPException):
        return None
    details = parse_details(cached["details"])
    category, name = cached["classification"], details.get("proposed_name")
    if category not in ALLOWED_CATEGORIES:
        return None
    if not isinstance(name, str) or unsafe_name_reason(name) or Path(name).suffix.lower() != path.suffix.lower():
        name = None
    return {"category": category, "proposed_name": name, "reason": str(details.get("reason") or ""), "model": details.get("model")}


def validate_action(source: Path, destination: Path, root: Path) -> tuple[str, str]:
    if not is_symlink_safe(source, root) or destination.is_symlink():
        return "blocked", "Symbolic links are not accepted for file operations."
    name_problem = unsafe_name_reason(destination.name)
    if name_problem:
        return "blocked", name_problem
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


def action_for(
    source: Path, root: Path, proposed_name: str | None = None, category: str | None = None,
    source_type: Literal["local-rule", "ollama"] = "local-rule", note: str = "",
) -> PlanAction:
    name = proposed_name or source.name
    category = category if category in ALLOWED_CATEGORIES else (suffix_category(source) or "other")
    # The category folder (one of a fixed set) sits beside the file; `root` is only the safety boundary.
    destination = source.parent / category / name
    name_problem = unsafe_name_reason(name)
    if name_problem:
        validation, reason = "blocked", name_problem
    else:
        validation, reason = validate_action(source, destination, root)
    stat = source.stat()
    action_id = hashlib.sha256(f"{root}:{source}:{destination}".encode()).hexdigest()[:16]
    return PlanAction(
        id=action_id, source=str(source), proposed_name=name, current_location=str(source.parent),
        proposed_destination=str(destination), source_type=source_type, validation=validation,
        reason=f"{reason} {note}".strip(), workspace_root=str(root), source_size=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns, source_fingerprint=identity_fingerprint(source), category=category,
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
    files, truncated = scan_directory(root)
    items, stats = classify_scan(root, files, request.use_ai)
    duration_ms = max(1, round((time.perf_counter() - started) * 1000))
    note = f" INCOMPLETE: stopped at the {MAX_SCAN_ENTRIES}-file limit" if truncated else ""
    record_activity("scan", f"Scanned {root} ({len(items)} files, {stats['cache_hits']} cache hits){note}")
    return {
        "path": str(root), "items": items, "count": len(items),
        "completeness": completeness(root, len(items), truncated),
        "processing": {**stats, "duration_ms": duration_ms},
    }


@app.post("/api/plans", response_model=PlanResponse)
def plans(request: PlanRequest) -> PlanResponse:
    if not request.files:
        raise HTTPException(status_code=400, detail="Select at least one file to create a plan.")
    root = resolved_directory(request.workspace_root)
    paths = resolve_plan_sources(root, request.files)
    actions = []
    connection = db()
    try:
        for path in paths:
            suggestion = plan_suggestion(connection, root, path)
            if suggestion:
                renamed = suggestion["proposed_name"] if suggestion["proposed_name"] != path.name else None
                note = f"Suggested by Ollama ({suggestion['model']}): {suggestion['reason']}".strip()
                actions.append(action_for(path, root, renamed, suggestion["category"], "ollama", note))
            else:
                actions.append(action_for(path, root))
    finally:
        connection.close()
    claimed: dict[str, int] = {}
    for index, action in enumerate(actions):
        if action.proposed_destination in claimed and action.validation != "blocked":
            action.validation = "blocked"
            action.reason = "Another proposal in this plan targets the same destination."
        claimed.setdefault(action.proposed_destination, index)
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
        # Keep the category (including a model-chosen one) while the name is user-edited.
        updated = action_for(source, Path(action.workspace_root), request.proposed_name, category=action.category or None)
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
    files, truncated = scan_directory(root)
    by_size: dict[int, list[Path]] = {}
    for path in files:
        try:
            by_size.setdefault(path.stat().st_size, []).append(path)
        except OSError:
            continue
    groups: dict[tuple[int, str], list[str]] = {}
    hashed = skipped_cloud = 0
    for size, same_size in by_size.items():
        if len(same_size) < 2:  # a file with a unique size cannot have an exact duplicate, so it is never read
            continue
        for path in same_size:
            if is_cloud_placeholder(path):  # hashing would download the file from the cloud
                skipped_cloud += 1
                continue
            try:
                fingerprint = content_fingerprint(path)
            except OSError:
                continue
            hashed += 1
            groups.setdefault((size, fingerprint), []).append(str(path))
    matches = [{"size": size, "fingerprint": fingerprint, "files": names} for (size, fingerprint), names in groups.items() if len(names) > 1]
    note = f", {skipped_cloud} cloud-only file(s) not read" if skipped_cloud else ""
    record_activity("duplicates", f"Found {len(matches)} exact duplicate group(s) in {root}{note}")
    return {
        "path": str(root), "groups": matches, "count": len(matches), "fingerprint": "SHA-256 content hash",
        "completeness": completeness(root, len(files), truncated),
        "files_hashed": hashed, "skipped_cloud_only": skipped_cloud,
    }


BENCHMARK_METHODOLOGY = (
    "Every phase classifies the same file list. 'No cache' applies the deterministic extension rules directly; "
    "'cold cache' is the first cached pass over an empty temporary SQLite cache; 'warm cache' is a second pass over the "
    "unchanged files. Each phase is repeated with a fresh temporary cache and the median is reported. The application "
    "database and its processing cache are neither read nor written by the timed phases. Deterministic extension "
    "lookup needs no cache, so a warm cache is not expected to beat it; the cache exists to avoid repeated model "
    "requests, which this benchmark does not exercise (model requests are always 0)."
)


def median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def timed(function: Any, *args: Any) -> tuple[Any, float]:
    started = time.perf_counter()
    result = function(*args)
    return result, (time.perf_counter() - started) * 1000


@app.post("/api/benchmarks", response_model=BenchmarkResponse)
def benchmark(request: BenchmarkRequest) -> BenchmarkResponse:
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        if request.synthetic_files:
            temporary = tempfile.TemporaryDirectory(prefix="lazydesk-benchmark-")
            root = Path(temporary.name).resolve()
            for index in range(request.synthetic_files):
                suffix = "dat" if index % 10 == 0 else "txt"  # a tenth have no deterministic extension rule
                (root / f"sample-{index:04d}.{suffix}").write_text(f"deterministic sample {index}\n", encoding="utf-8")
        elif request.path:
            root = resolved_directory(request.path)
        else:
            raise HTTPException(status_code=400, detail="Provide a workspace path or synthetic_files count.")
        (files, truncated), discovery_ms = timed(scan_directory, root)
        samples: dict[str, list[float]] = {"no_cache": [], "cold_cache": [], "warm_cache": []}
        results_match, cold_stats, warm_stats = True, {}, {}

        def signature(items: list[dict[str, Any]]) -> list[tuple[str, str]]:
            return [(item["relative_path"], item["category"]) for item in items]

        for _ in range(request.repetitions):
            baseline_items, elapsed = timed(classify_without_cache, root, files)
            samples["no_cache"].append(elapsed)
            with tempfile.TemporaryDirectory(prefix="lazydesk-benchmark-cache-") as cache_dir:
                cache = db(Path(cache_dir) / "cache.sqlite")
                try:
                    (cold_items, cold_stats), elapsed = timed(classify_scan, root, files, False, cache)
                    samples["cold_cache"].append(elapsed)
                    (warm_items, warm_stats), elapsed = timed(classify_scan, root, files, False, cache)
                    samples["warm_cache"].append(elapsed)
                finally:
                    cache.close()
            results_match = results_match and signature(baseline_items) == signature(cold_items) == signature(warm_items)
        medians = {name: round(median(values), 3) for name, values in samples.items()}
        count = len(files)
        phases = {
            "cold": {"files": count, "processed": count, "cache_hits": 0, "duration_ms": medians["no_cache"]},
            "optimized_cold": {"files": count, "processed": cold_stats["cache_misses"], "cache_hits": cold_stats["cache_hits"], "duration_ms": medians["cold_cache"]},
            "warm": {"files": count, "processed": warm_stats["cache_misses"], "cache_hits": warm_stats["cache_hits"], "duration_ms": medians["warm_cache"]},
        }
        warm_ms = medians["warm_cache"]
        payload = BenchmarkResponse(
            id=hashlib.sha256(f"{root}:{utc_now()}".encode()).hexdigest()[:16], created_at=utc_now(),
            path=str(root), mode="synthetic" if temporary else "workspace", cold=phases["cold"], warm=phases["warm"],
            optimized_cold=phases["optimized_cold"], samples={name: [round(v, 3) for v in values] for name, values in samples.items()},
            baseline_duration_ms=max(1, round(medians["no_cache"])), optimized_duration_ms=max(1, round(warm_ms)),
            files_discovered=count, unchanged_files_skipped=warm_stats["cache_hits"],
            cache_hits=warm_stats["cache_hits"], cache_misses=warm_stats["cache_misses"], local_model_requests=0,
            throughput=round(count / (warm_ms / 1000), 2) if warm_ms else 0,
            classification_quality="Deterministic categories; no labeled accuracy set supplied.",
            discovery_ms=round(discovery_ms, 3), results_match=results_match,
            speedup_warm_vs_baseline=round(medians["no_cache"] / warm_ms, 2) if warm_ms else 0,
            methodology=BENCHMARK_METHODOLOGY + (f" Discovery stopped at the {MAX_SCAN_ENTRIES}-file limit." if truncated else ""),
        )
        connection = db()
        connection.execute("INSERT INTO benchmarks VALUES (?, ?, ?)", (payload.id, payload.created_at, payload.model_dump_json()))
        connection.commit()
        connection.close()
        record_activity("benchmark", f"Benchmarked {root} ({count} files, {request.repetitions} repetition(s))")
        return payload
    finally:
        if temporary:
            temporary.cleanup()


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
