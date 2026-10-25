# LazyDesk AI Studio

LazyDesk AI Studio is a local-first desktop-style automation workspace. It uses a custom React/Vite frontend, a FastAPI backend, SQLite processing-cache persistence, deterministic routing, exact duplicate detection, reproducible benchmarks, and optional local Ollama classification.

## Run locally

### Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`. The backend binds to the local development interface and all filesystem requests are validated server-side.

## API surface

- `GET /api/health` — backend, installed Ollama models, and selected model status.
- `GET /api/settings` and `PATCH /api/settings` — inspect or select an already-installed local Ollama model.
- `POST /api/scan` — recursively scan one explicitly selected directory and reuse/invalidate the SQLite processing cache.
- `POST /api/plans` — create a rule-based review plan from selected file paths.
- `PATCH /api/plans/{plan_id}/actions/{action_id}` — edit a proposed filename and immediately revalidate it.
- `POST /api/plans/{plan_id}/approve` — persist explicit approvals for non-blocked proposals from that plan.
- `POST /api/plans/{plan_id}/reject` — persist explicit rejections for proposals.
- `POST /api/execute` — move approved files only from the specified plan after re-validating source identity, destination availability, symlink safety, and workspace boundaries.
- `POST /api/duplicates` — find exact duplicate groups by SHA-256 content fingerprint; no deletion is performed.
- `GET /api/activity` — recent local activity.
- `POST /api/benchmarks` and `GET /api/benchmarks` — measured cold/warm benchmark and history; pass `synthetic_files` for reproducible fixture data.

For ambiguous files, the scan uses `OLLAMA_HOST` and the explicitly configured `OLLAMA_MODEL` only when that model is already installed. It never downloads a model and sends filename/extension/size metadata, not file contents. Easy extensions use deterministic rules and avoid inference calls. Invalid or unavailable Ollama responses fall back to a clearly labelled local rule.

The review queue never mutates files during discovery, planning, or editing. Execution is a separate, explicit request and re-checks source identity, destination availability, symlink state, and the workspace boundary immediately before moving anything.

The frontend includes dedicated Duplicates and Settings views. Duplicates reports exact content groups only, while Settings lists only models returned by the local Ollama `/api/tags` endpoint.

## Tests

```powershell
cd backend
pytest
```

```powershell
cd frontend
npm test
npm run build
```
