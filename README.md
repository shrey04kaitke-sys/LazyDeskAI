# LazyDesk AI Studio

A local-first file organizer. It scans a folder you choose, proposes where each file should go, and moves nothing until you review, approve, and apply the proposals yourself. The React frontend talks to a FastAPI backend on `127.0.0.1`; state lives in a local SQLite file. Nothing is uploaded anywhere, and the optional local Ollama model only ever sees file *names, extensions and sizes*, never file contents.

## Architecture

```
frontend/ (React + TypeScript + Vite + Tailwind, http://127.0.0.1:5173)
   │  JSON over HTTP (http://127.0.0.1:8000/api by default; see LAZYDESK_PORT)
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

1. **Scan** (`POST /api/scan`): files in the selected folder are classified. Files with a known extension use a fixed rule (`.pdf` → `documents`, `.jpg` → `images`, …). Files with no rule go to the optional Ollama model **only if** a model is chosen (your Settings/`OLLAMA_MODEL` choice, otherwise the built-in default `qwen2.5:3b`) *and* installed. The result is cached in SQLite.
2. **The model only ever suggests, and its answer is validated by code, not by the prompt.** The file name is sent to the model as quoted JSON data (never mixed into the instructions), and names that read like instructions (for example "Ignore previous instructions…") are **never sent** — the deterministic rule decides. The reply must parse, match a schema, and pass independent checks: the category must be one of seven fixed folders and may not contradict an extension rule; an optional *suggested name* must be a single Windows-safe file name with the same extension, with no executable/script extension (`.exe`, `.bat`, `.ps1`, …), and not copied out of the original file name. A reply that fails any of these — malformed JSON, wrong schema, unknown category, `../x`, `autoexec.bat.txt`, `CON.txt` — is a **rejected answer**: the rule decides, and the rejection is cached as a final result, so the same file is **not sent to the model again** on later scans. The cache entry records only the model name, a code-generated reason, and a fingerprint of the prompt and validation rules — never text written by the model — and it can never supply a name or category. It is invalidated, and the model asked again, when the file's size, modification time or content changes, the file is renamed, a different model is selected, or the prompt, validation rules or cache version change. Transient problems (timeout, unreachable server, warm-up failure, request budget or breaker) are *not* cached as final and are retried next scan.
   Bounds per scan: a not-yet-loaded model gets **one** warm-up of at most 60 s (skipped if it is already loaded; if it cannot load, the scan uses the rules), then each request is limited to 20 s, at most 20 requests, a stop after 2 failures in a row, and 180 s of total model time.
3. **Plan** (`POST /api/plans`): the selected files become proposals *within the folder you scanned* (the "workspace root"). A still-valid cached model suggestion (same file size, mtime, fingerprint, model) can set the **category folder**; the **file name always stays the original**. A model-suggested name is shown as an option and is applied only if you click "Use suggested name" — which then voids any approval, so you must approve again. The destination is always `<the file's folder>/<category>/<name>`, where category is one of seven fixed folder names, and execution re-checks that the folder matches the category independently of how it was chosen.
4. **Review**: edit a name (this voids any earlier approval), approve, or reject. Rejected proposals can never be approved or applied.
5. **Apply** is two steps. `POST /api/plans/{id}/preview` re-checks every action and returns the exact moves (source, destination, rename, category, size, fingerprint) plus a single-use confirmation that expires after 2 minutes and is bound to that plan revision, those actions, and their paths and fingerprints. The UI lists the moves in a dialog whose focused button is **Cancel**. `POST /api/execute` needs that confirmation: if anything changed since the preview (an edit, a file, the destination, the workspace policy) it answers `409` and moves nothing.
6. **Journal and Undo.** Before each move LazyDesk commits a journal record (SQLite, `synchronous=FULL`) and updates it right after; a run is `completed` only when every move is `moved`. After a crash, startup settles each interrupted move from the file system: unambiguous ones are recorded, ambiguous ones become `needs_attention` and are never moved automatically. `POST /api/runs/{id}/undo` is **best-effort recovery, not a guarantee**: see below.

## Safety model

| Guarantee | How it is enforced (and tested) |
|---|---|
| Nothing moves without explicit approval then an explicit apply | Review, approve and apply are separate requests; the backend rejects unapproved, rejected, blocked and already-applied actions regardless of what the UI sends |
| Files stay inside the scanned folder | `workspace_root` is required, never inferred; sources and destinations are resolved and must lie inside it; `..`, sibling look-alike folders and symlinked directories are rejected |
| Stale proposals cannot run | Size, modified time and (when available) SHA-256 of the source are re-checked at approval **and** at apply |
| No overwrites | The destination must not exist at apply time; duplicate destinations in one plan are blocked |
| Model output cannot cause harm | Category is from a fixed list; names are validated and keep the extension; a failure only means "use the rule" |
| Cloud files are not downloaded | OneDrive-style placeholders (and files > 64 MiB) are identified by size + mtime instead of being hashed for caching and plan identity; duplicate detection skips cloud-only files and only hashes files that share a size |
| Approval covers what you reviewed | Every change to an action bumps its revision. The UI sends the revisions it showed when approving; if the server's differ the approval is a `409`. The Apply confirmation is bound to the plan revision and each action's paths and fingerprints |
| One change at a time | A per-plan lock serialises edit, approve, reject, preview, Apply and Undo. A second Apply of the same plan never waits and never runs; a stored `running` run also blocks a new one |
| Apply is recorded durably | A per-file journal (`apply_runs`, `apply_moves`) is committed before and after every move; partial failures stop, record done/failed/not-attempted, and are reported |
| Incomplete scans are never presented as complete | Every scan returns `completeness`; the UI shows a red warning when the 10,000-file cap was reached |

**Limitations — read these.** Applying is **not atomic**: if a move fails midway, earlier moves stay applied and are reported, later ones are not attempted. **Undo is best-effort recovery, not a guarantee.** It moves files back in reverse order only if *every* one is still exactly as LazyDesk left it (a regular, non-link file at the destination with the recorded size, modified time and SHA-256) and its original path is free with a valid parent folder. Otherwise it refuses, records why (`undo_blocked`), and changes nothing. It never overwrites or deletes a file, removes only folders the run created and only when empty (never recursively), and will not touch a run that `needs_attention`. Undo cannot help if a file was edited, moved or deleted afterwards. A tiny window exists between the pre-check and each move, and between a crash and the next start's reconciliation; a run left `needs_attention` must be checked by hand. Symlink-escape protection is skipped by the 4 symlink tests on Windows without symlink rights; CI runs them on Linux.

**Plans created by older versions** are never trusted for their missing metadata. When applied they are re-derived and re-checked with the current rules (category folder from the extension rule, filename, workspace boundary, symlinks, size/mtime, approval), and a plan that predates content fingerprints is refused with an explanation until you create a new one. Stored plans are not modified.

**About the model and prompt injection — read this too.** What the model says is shown as *unverified, based on the file name and size only* (it never reads file contents), and it can be wrong or hallucinate. Prompt injection is **mitigated, not solved**: a filename can still steer the model to a *valid* category (for example an instruction-like name that slips past the filter), and that is then a labeled, pending suggestion for you to review inside your workspace — it cannot rename a file, escape the folder, or move anything by itself. The instruction filter is a heuristic and can be evaded. A manual test of three local models (10 synthetic files each) found 30/30 valid JSON and every model obeying one injection filename; this version's changes (system prompt, quoted metadata, warm-up, name handling) were covered by mocked tests and have **not yet been re-validated against a real model**. CI never contacts Ollama.

### Protected workspace folders

LazyDesk refuses to use these as a workspace, wherever the request comes from (scan, plan creation, duplicates, benchmarks) and again when a plan is approved or applied, so a stale or edited plan cannot bypass it. There is no override: not in the UI, the API, or an environment variable.

- Drive roots, the Windows folder, Program Files, ProgramData, the Users folder itself, other users' profiles, your own profile root, and AppData.
- Credential folders (`.ssh`, `.aws`, `.gnupg`, `.azure`, `.kube`, `.docker`, ...) at any depth, plus recycle-bin and system-volume folders.
- Network paths (UNC, `\\?\UNC\`), device paths and unsupported prefixes. These are refused from the text alone, without touching the network.
- Any folder that contains LazyDesk's own database.
- On Linux/macOS: `/`, `/etc`, `/usr`, `/bin`, `/boot`, `/dev`, `/proc`, `/sys`, `/root`, `/System`, `/Applications`, `/home` and `/Users` themselves, and other users' homes.

Paths are compared component by component after resolving junctions, symlinks, `..`, short names, trailing dots/spaces and `\\?\` prefixes, case-insensitively on Windows. `C:\WindowsX` and `C:\Users\me2` are not mistaken for `C:\Windows` or `C:\Users\me`. Documents, Downloads, Desktop and project folders keep working. A protected subfolder inside an allowed workspace is skipped, never read, and listed in the scan summary; directory links inside a workspace are not followed and are counted.

Limits: network drives are detected on Windows only (POSIX network mounts are not). A folder can still change between the check and the move; the recheck at apply time narrows that window but does not eliminate it. A workspace that contains the app's database, such as a parent folder of this repo, is refused outright rather than partially scanned: pick a subfolder.

### Local API protection

The backend listens on `127.0.0.1` only (every launcher passes `--host 127.0.0.1`; none listens on all interfaces). Because a web page you visit can still send requests to a loopback port, every request is checked before routing and before any body is read:

1. **Peer** must be a loopback address.
2. **Host** must be exactly one header, `127.0.0.1`, `localhost` or `[::1]` (any letter case) on the configured backend port (`LAZYDESK_PORT`, default 8000). Suffix tricks such as `127.0.0.1.evil.com`, a missing port, `08000` or userinfo (`@`) are refused. This blocks DNS rebinding.
3. **Origin**, when sent, must be a LazyDesk frontend origin (`http://127.0.0.1|localhost|[::1]:5173`, port from `LAZYDESK_FRONTEND_PORT`), and `Sec-Fetch-Site`, when sent, must be `same-origin` or `same-site`.
4. **Token**: every route except `GET /api/health` and the bootstrap needs the per-launch token in the `X-LazyDesk-Token` header (constant-time comparison; never accepted in a URL, cookie or `Authorization`).

Rejections are generic (`Request rejected.` / `Authorization required.`), happen before the database or files are touched, and never include the token.

**Bootstrap.** At startup the backend creates a random token (`secrets.token_urlsafe(32)`), held in memory only, new for every launch. The frontend calls `POST /api/session/bootstrap`; the backend answers only if Host, peer and an allowed `Origin` all check out, with `Cache-Control: no-store`. The frontend keeps the token in a JavaScript variable only: not in storage, cookies, URLs or logs, and not in the built bundle. After a backend restart the next request gets `401`, the frontend fetches a fresh token once, and otherwise shows a clear "reload" message instead of sending anything unauthenticated.

**Other surface.** `/api/health` returns only `{"status": "ok"}`; model and Ollama details moved to the authorized `/api/status`. `/docs`, `/redoc` and `/openapi.json` are not served. CORS allows only the frontend origins, `GET/POST/PATCH`, no credentials, and is not the authorization mechanism.

**What this does not protect against:** malware, browser extensions or any other process running as your user. They can read process memory, call the bootstrap with a forged `Origin`, or read the files LazyDesk can read. It also does not defend against someone with your OS account or against a compromised browser profile. It is meant to stop *other websites* and *other machines* from driving your local backend. Use `127.0.0.1:5173` for the UI; if you change the ports, set `LAZYDESK_PORT` / `LAZYDESK_FRONTEND_PORT` to match (see Ports below).

## Run locally (Windows PowerShell)

**Requirements:** Python 3.12 or newer (the backend uses `Path.is_junction` / `DirEntry.is_junction`, added in 3.12; the backend suite is run on 3.12 and 3.13) and Node.js (the CI workflow uses Node 24; other versions are untested). Windows is the primary platform; on Linux only the backend test suite is exercised.

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

Open `http://127.0.0.1:5173` (use `127.0.0.1`, not `localhost`). Optional: install [Ollama](https://ollama.com) and pull a model yourself; LazyDesk never downloads models. **Default model:** if nothing is configured and `qwen2.5:3b` is installed, it is used for files that have no extension rule (it was the most consistent of the three models in a small synthetic test of 10 files each — that is not evidence of general accuracy). Anything you configure wins, in this order: a model saved under **Settings** → `OLLAMA_MODEL` (empty means no model) → "Deterministic fallback" saved in Settings → the built-in default. Set `LAZYDESK_DEFAULT_MODEL` to another name, or to an empty value to switch the built-in default off. If the model isn't installed or Ollama isn't running, scans are deterministic. Environment variables: `OLLAMA_HOST` (default `http://127.0.0.1:11434`), `OLLAMA_MODEL`, `LAZYDESK_DEFAULT_MODEL` (default `qwen2.5:3b`), `LAZYDESK_OLLAMA_TIMEOUT` (per request, seconds, default 20), `LAZYDESK_OLLAMA_COLD_START_TIMEOUT` (one warm-up per scan, default 60), `LAZYDESK_OLLAMA_SCAN_BUDGET` (total model seconds per scan, default 180), `LAZYDESK_DB` (database path).

**Ports.** The backend and the UI must agree. Start both with the same settings: `LAZYDESK_PORT` (backend, default 8000; pass the same number to `--port`) and `LAZYDESK_FRONTEND_PORT` (Vite dev server, default 5173, `strictPort`). For example, in PowerShell: `$env:LAZYDESK_PORT=9000; uvicorn app.main:app --host 127.0.0.1 --port 9000` and, in another terminal, `$env:LAZYDESK_PORT=9000; npm run dev`. An unsupported value (not a whole number 1-65535) stops the backend at startup and the dev server, and the UI shows the problem and sends no requests. If the server listens on a different port than `LAZYDESK_PORT`, requests are rejected with `400` and the backend logs one line saying so. Only those two values are injected into the frontend bundle.

**One backend per database.** Crash recovery and the run registry assume nothing else is executing runs against the same database, so only one backend process may use a given database at a time. At startup the backend takes an exclusive operating-system lock on a lock file beside the database (`<database>.lock`; `msvcrt` on Windows, `fcntl.flock` elsewhere) **before** crash recovery runs. A second backend pointed at the same database refuses to start with an error that says so, and it reads and changes nothing. The lock is held for the life of the process and released on shutdown; if the process crashes or is killed, the operating system releases it, so there is nothing to delete by hand (the lock file itself may stay on disk; its existence means nothing, only the lock does). Different database paths are independent, so a demo backend (its own database) is not blocked by a normal one, though both still need different ports. If the lock cannot be taken at all (unusable path, permissions, a filesystem without locking) startup is refused rather than continuing unprotected. Locking on network shares and unusual filesystems can behave differently or be unsupported, so keep the database on a local disk. Hard links to one database from other folders are not detected.

> Starting this version on an existing database adds one nullable column (`processing_cache.details`) and treats older cache rows as stale, so the first scan re-processes files. No other data is changed.

## Demo (synthetic data, isolated database)

The demo never touches your files or `backend/lazydesk.db`. It lives in `%USERPROFILE%\LazyDeskDemo` (not AppData, which LazyDesk refuses as a workspace; an older demo under `%LOCALAPPDATA%\LazyDeskDemo` is left untouched and can no longer be scanned, so re-run `setup-demo.ps1`): a 22-file workspace (nested folders up to 4 levels, 4 exact-duplicate pairs, same-named-but-different files, files with no extension rule) plus its own `demo.sqlite`. If PowerShell blocks scripts, prefix commands with `powershell -ExecutionPolicy Bypass -File`.

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
6. Optional, with Ollama running and `qwen2.5:3b` (the default) or another model you chose in **Settings** installed: re-scan; `mystery.xyz` / `data.weird` / `readme` may now show an "AI" badge, and their review proposals name the model.

## Tests

```powershell
cd backend
pytest                      # all tests use a temporary database; backend/lazydesk.db is never opened
cd ..\frontend
npm test                    # vitest
npx tsc -p tsconfig.app.json --noEmit
npm run build               # writes frontend/dist (git-ignored)
```

Backend coverage: review/approval/execution regressions (rejected, unapproved, stale, conflicts, traversal, partial failure), workspace-root and scan-completeness behavior, the full Ollama flow with mocked responses (valid, malformed, unsafe, slow, unavailable, budget, model change), prompt-injection regressions (instruction-like names, executable and path-like suggestions, category/extension contradictions, tampered plans), explanation wording, and cold-start behavior (warm-up, timeouts, breaker, time budget), cache invalidation and migration, duplicate detection, benchmark accuracy and isolation, and the demo's setup/reset safety guards plus an end-to-end scan → duplicates → review walkthrough.

Also covered: protected workspace folders (`test_workspace_policy.py`), the local API guard (`test_api_guard.py`), Apply preview and confirmation (`test_apply_confirmation.py`), the journal, crash recovery and Undo (`test_journal_undo.py`), recovery-versus-Apply interleavings forced with events (`test_reconcile_race.py`), plus locking, the explicit "deterministic fallback" setting and non-default ports with a real throw-away server (`test_concurrency_ports_settings.py`). All use temporary databases and synthetic folders. Browser-level behavior is not covered by automated tests, and GitHub CI results are whatever the Actions tab shows for the pushed commit.

### Changing the validation code: the rules-version contract

Cached model answers and remembered rejections are reused only while three things match: the model's **name and weights digest** (so re-pulling `qwen2.5:3b` with new weights re-asks everything), the **rules fingerprint** (a hash of the prompt, options, categories, extension lists and patterns), and `RULES_VERSION`. The fingerprint cannot see changes to validation *logic*, so there is an explicit contract: `tests/test_rules_contract.py` pins a hash of the guarded validation functions. If you change their code, that test fails with instructions — increment `RULES_VERSION` in `backend/app/main.py` and update the pin. Cosmetic edits (comments, blank lines, line endings) don't trip it. If the server does not report digests, matching degrades to the model name only.

File names are checked before anything is accepted: a *new* name (typed by you or suggested by a model) is refused if it contains invisible, bidirectional-control or private-use characters (they can make `report‮txt.pdf` display as something else), Windows device names in any spelling (`CON`, `COM¹`, `CONIN$`, trailing-space and fullwidth variants), or a lookalike dot hiding an executable extension. A file that *already* has such a character in its name is flagged with a warning rather than refused or renamed.

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

Everything except `GET /api/health` and `POST /api/session/bootstrap` needs the `X-LazyDesk-Token` header (see Local API protection). `GET /api/health` (liveness only) · `POST /api/session/bootstrap` · `GET /api/status` (Ollama and model details) · `GET|PATCH /api/settings` · `POST /api/scan` · `POST /api/plans` (requires `workspace_root`) · `PATCH /api/plans/{id}/actions/{action}` · `POST /api/plans/{id}/approve|reject` · `POST /api/execute` · `GET /api/plans/{id}` · `POST /api/plans/{id}/preview` · `GET /api/runs` · `GET /api/runs/{id}` · `POST /api/runs/{id}/undo` · `POST /api/duplicates` · `POST|GET /api/benchmarks` · `GET /api/activity`. There are no interactive docs or OpenAPI endpoint.

## Known gaps

No folder-tree navigation or confidence-based routing; undo is best-effort (see above); the confirmation token and per-plan locks live in one backend process (a second backend on the same database is refused at startup, see "One backend per database"); browser/UI behavior is covered by type-checking, unit tests and manual steps only (no automated browser tests); Ollama behavior with this version's prompt and warm-up has not been re-validated against a real model; prompt-injection resistance is a heuristic filter plus output validation, not a guarantee; the file list is capped at 10,000 entries per scan.
