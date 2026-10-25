# LazyDesk AI Studio

A local-first file organizer. It scans a folder you choose, proposes where each file should go, and moves nothing until you review, approve, and apply the proposals yourself. The React frontend talks to a FastAPI backend on `127.0.0.1`; state lives in a local SQLite file. Nothing is uploaded anywhere, and the optional local Ollama model only ever sees file *names, extensions and sizes*, never file contents.

## Architecture

```
frontend/ (React + TypeScript + Vite + Tailwind, http://127.0.0.1:5173)
   │  JSON over HTTP (hard-wired to http://127.0.0.1:8000/api)
backend/app/main.py (FastAPI, http://127.0.0.1:8000)
   ├─ scan        recursive discovery (10,000-file cap, reported when hit) → classify → processing cache
   ├─ classify    1. extension rules (deterministic)   2. optional Ollama, metadata only, validated, bounded
   ├─ plans       a review plan = proposals bound to one workspace root; edit / approve / reject / apply
   ├─ execute     re-validates every action against the live filesystem, then moves files one at a time
   ├─ duplicates  exact SHA-256 groups (size-grouped first; cloud-only files are not read)
   └─ benchmarks  isolated, repeatable timing (see "Benchmark methodology")
SQLite (backend/lazydesk.db, git-ignored): activity, plans, processing cache, benchmark history, settings
demo/ synthetic workspace + isolated demo database scripts (outside the repo, never your real data)
```

### How a file becomes a proposal

1. **Scan** (`POST /api/scan`): files in the selected folder are classified. Files with a known extension use a fixed rule (`.pdf` → `documents`, `.jpg` → `images`, …). Files with no rule go to the optional Ollama model **only if** one is selected *and* installed. The result is cached in SQLite.
2. **Model output is validated** before it is trusted: JSON must parse and match a schema (category from a fixed list, a plain file name, a short reason). The suggested name must be a single, Windows-safe file name with the **same extension** as the original. Anything else — unavailable server, timeout, malformed JSON, unknown category, `../x`, `CON.txt` — silently falls back to the deterministic rule and is retried on the next scan. After 2 consecutive failures the model is skipped for the rest of that scan, and at most 20 requests are made per scan.
3. **Plan** (`POST /api/plans`): the selected files become proposals *within the folder you scanned* (the "workspace root"). A still-valid cached model suggestion (same file size, mtime, fingerprint, model) sets the category folder and name; otherwise the rule is used. The destination is always `<the file's folder>/<category>/<name>`, where category is one of seven fixed folder names.
4. **Review**: edit a name (this voids any earlier approval), approve, or reject. Rejected proposals can never be approved or applied.
5. **Apply** (`POST /api/execute`): only approved proposals from that plan. Before anything moves, **every** action in the batch is re-checked; if any fails, nothing moves.

## Safety model

| Guarantee | How it is enforced (and tested) |
|---|---|
| Nothing moves without explicit approval then an explicit apply | Review, approve and apply are separate requests; the backend rejects unapproved, rejected, blocked and already-applied actions regardless of what the UI sends |
| Files stay inside the scanned folder | `workspace_root` is required, never inferred; sources and destinations are resolved and must lie inside it; `..`, sibling look-alike folders and symlinked directories are rejected |
| Stale proposals cannot run | Size, modified time and (when available) SHA-256 of the source are re-checked at approval **and** at apply |
| No overwrites | The destination must not exist at apply time; duplicate destinations in one plan are blocked |
| Model output cannot cause harm | Category is from a fixed list; names are validated and keep the extension; a failure only means "use the rule" |
| Cloud files are not downloaded | OneDrive-style placeholders (and files > 64 MiB) are identified by size + mtime instead of being hashed for caching and plan identity; duplicate detection skips cloud-only files and only hashes files that share a size |
| Incomplete scans are never presented as complete | Every scan returns `completeness`; the UI shows a red warning when the 10,000-file cap was reached |

**Limitations — read these.** Applying is **not atomic and not reversible**: if a move fails midway, earlier moves stay applied and are reported, later ones are not attempted. There is no undo. A tiny window exists between the pre-check and each move. Symlink-escape protection is implemented but could not be exercised on the Windows machine used for development (4 symlink tests are skipped there; run the suite where symlinks are available to cover them). Ollama integration is tested **only with mocked responses**; no real model inference has been verified.

## Run locally (Windows PowerShell)

```powershell
# Backend (terminal 1)
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000 --host 127.0.0.1

# Frontend (terminal 2)
cd frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173` (use `127.0.0.1`, not `localhost`). Optional: install [Ollama](https://ollama.com), pull a model yourself, and pick it under **Settings**; LazyDesk never downloads models. Environment variables: `OLLAMA_HOST` (default `http://127.0.0.1:11434`), `OLLAMA_MODEL`, `LAZYDESK_OLLAMA_TIMEOUT` (seconds, default 20), `LAZYDESK_DB` (database path).

> Starting this version on an existing database adds one nullable column (`processing_cache.details`) and treats older cache rows as stale, so the first scan re-processes files. No other data is changed.

## Demo (synthetic data, isolated database)

The demo never touches your files or `backend/lazydesk.db`. It lives in `%LOCALAPPDATA%\LazyDeskDemo`: a 22-file workspace (nested folders up to 4 levels, 4 exact-duplicate pairs, same-named-but-different files, files with no extension rule) plus its own `demo.sqlite`. If PowerShell blocks scripts, prefix commands with `powershell -ExecutionPolicy Bypass -File`.

```powershell
.\demo\setup-demo.ps1            # create the synthetic workspace (prints its path)
.\demo\start-demo.ps1            # backend on 127.0.0.1:8000 using the DEMO database (stop your normal backend first)
cd frontend; npm run dev         # in a second terminal
.\demo\reset-demo.ps1            # restore the original files and wipe the demo database
.\demo\reset-demo.ps1 -Remove    # delete the demo workspace, database and marker
```

Safety: the scripts only operate on a folder named `LazyDeskDemo` that carries a marker file; they refuse anything else, drive roots, your home folder, linked folders, and anything under OneDrive, Desktop, Downloads or Documents.

**Walkthrough** (nothing is approved or applied automatically):

1. Open `http://127.0.0.1:5173`, paste the workspace path from `setup-demo`, click **Scan folder** → 22 files, no incomplete-scan warning.
2. **Duplicates → Scan duplicates** → 4 exact groups (invoice, budget, report, logo).
3. **Files**: select `Projects/Alpha/assets/logo.png`, `Projects/Beta/notes.txt`, `Archive/2023/mystery.xyz` → **Review**. Observe: the workspace boundary is the demo root even for nested files; destinations are `…/images/`, `…/documents/`, `…/other/` next to each file; every proposal is *pending* with a validation badge.
4. Edit a name inline, try `..\x.txt` (blocked), reject one. **Do not click Apply** unless you want the demo files moved — `reset-demo.ps1` puts everything back.
5. **Performance → Run synthetic (200 files)** to see the measured phases.
6. Optional, with Ollama running and a model selected in **Settings**: re-scan; `mystery.xyz` / `data.weird` / `readme` may now show an "AI" badge, and their review proposals name the model.

## Tests

```powershell
cd backend
pytest                      # all tests use a temporary database; backend/lazydesk.db is never opened
cd ..\frontend
npm test                    # vitest
npx tsc -p tsconfig.app.json --noEmit
npm run build               # writes frontend/dist (git-ignored)
```

Backend coverage: review/approval/execution regressions (rejected, unapproved, stale, conflicts, traversal, partial failure), workspace-root and scan-completeness behavior, the full Ollama flow with mocked responses (valid, malformed, unsafe, slow, unavailable, budget, model change), cache invalidation and migration, duplicate detection, benchmark accuracy and isolation, and the demo's setup/reset safety guards plus an end-to-end scan → duplicates → review walkthrough.

## Benchmark methodology

`POST /api/benchmarks` (UI: **Performance**) classifies the **same file list** three ways and reports the **median of N repetitions** (default 3, max 9):

1. **No cache** — deterministic extension rules applied directly.
2. **Cold cache** — first pass through the real caching pipeline, against an *empty temporary* SQLite cache.
3. **Warm cache** — a second pass over the unchanged files (every file should be a cache hit).

The application database and its cache are never read or written by the timed phases (a fresh temporary cache is used per repetition), so a result can't be skewed by earlier scans. Each run also verifies all three phases produced identical categories (`results_match`) and reports exact hit/processed counts. Synthetic mode creates temporary files (90% `.txt`, 10% `.dat`) and deletes them afterwards.

**Measured on the development machine** (Windows 11, Python 3.13.0, SQLite 3.45.3, 9 repetitions, medians):

| Files | No cache | Cold cache | Warm cache | Warm hits | Warm vs no cache |
|---|---|---|---|---|---|
| 50 | 3.1 ms | 12.7 ms | 9.2 ms | 50/50 | 0.33× (slower) |
| 200 | 10.0 ms | 47.4 ms | 35.2 ms | 200/200 | 0.29× (slower) |
| 1000 | 51.6 ms | 231.8 ms | 188.5 ms | 1000/1000 | 0.27× (slower) |

**Interpretation, honestly:** for deterministic classification the cache is *slower* than not caching — a dictionary lookup beats a SQLite read, and the warm pass still stats every file. The cache is not a speed-up for rules; its purpose is to avoid repeating **model requests** (verified by tests: a second scan makes 0 requests and keeps the model's answers), and to remember results across sessions. That benefit is **not timed** here because no real model was available; model requests in the benchmark are always 0. Earlier informal figures from development ("84 ms cold vs 52 ms baseline") have no stored record and compared against a baseline that skipped the cached path's work; they should not be quoted. Rerun the benchmark on your machine — absolute numbers vary.

## API

`GET /api/health` · `GET|PATCH /api/settings` · `POST /api/scan` · `POST /api/plans` (requires `workspace_root`) · `PATCH /api/plans/{id}/actions/{action}` · `POST /api/plans/{id}/approve|reject` · `POST /api/execute` · `POST /api/duplicates` · `POST|GET /api/benchmarks` · `GET /api/activity`. Interactive docs at `http://127.0.0.1:8000/docs` while the backend runs.

## Known gaps

No folder-tree navigation or confidence-based routing; no undo; the frontend API address is fixed to port 8000; browser/UI behavior is covered by type-checking, unit tests and manual steps only (no automated browser tests); Ollama behavior with a real model is unverified; the file list is capped at 10,000 entries per scan.
