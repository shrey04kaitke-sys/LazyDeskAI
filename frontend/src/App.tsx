import { useEffect, useMemo, useState } from "react";
import { ApiError, createApiClient } from "./apiClient";
import { resolveApiConfig } from "./apiConfig";
import { countsText, expiryNote, fallbackControl, moveLine, statusLabel } from "./applyFlow";
import type { Preview, RunSummary } from "./applyFlow";
import type { ReactNode } from "react";
import {
  Activity,
  AlertTriangle,
  Archive,
  ArrowDownUp,
  BarChart3,
  Check,
  ChevronRight,
  ClipboardCheck,
  Copy,
  Clock3,
  File,
  FileArchive,
  FileCode2,
  FileImage,
  FileText,
  Folder,
  FolderOpen,
  HardDrive,
  LayoutDashboard,
  LoaderCircle,
  Menu,
  Play,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  X,
} from "lucide-react";

type View = "workspace" | "files" | "review" | "duplicates" | "activity" | "performance" | "settings";
type FileItem = {
  id: string; name: string; path: string; relative_path: string; extension: string;
  size: number; modified_at: string; is_directory: boolean;
  category?: string; classification_source?: string; classification_reason?: string;
};
type PlanAction = {
  id: string; source: string; proposed_name: string; current_location: string;
  proposed_destination: string; source_type: "local-rule" | "ollama";
  validation: "safe" | "warning" | "blocked"; reason: string; status: "pending" | "approved" | "rejected" | "executed";
  category?: string; suggested_name?: string | null; name_source?: "original" | "user" | "model-suggestion";
  explanation_basis?: "extension-rule" | "filename-and-size"; model?: string | null; model_note?: string | null;
  plan_schema?: number; revision?: number;
};

export const CURRENT_PLAN_SCHEMA = 2;
/** Plans stored by an older version are re-checked with today's rules and may be refused until they are recreated. */
export function isLegacyPlan(action: Pick<PlanAction, "plan_schema">) {
  return (action.plan_schema ?? 0) < CURRENT_PLAN_SCHEMA;
}
type Plan = { id: string; created_at: string; actions: PlanAction[] };
type Benchmark = {
  id: string; created_at: string; files_discovered: number; unchanged_files_skipped: number;
  cache_hits: number; cache_misses: number; local_model_requests: number; optimized_duration_ms: number;
  baseline_duration_ms: number; throughput: number; classification_quality: string; path: string;
  mode: "workspace" | "synthetic"; cold: { files: number; processed: number; cache_hits: number; duration_ms: number };
  warm: { files: number; processed: number; cache_hits: number; duration_ms: number };
  optimized_cold?: { files: number; processed: number; cache_hits: number; duration_ms: number };
  samples?: Record<string, number[]>; discovery_ms?: number; results_match?: boolean;
  speedup_warm_vs_baseline?: number; methodology?: string;
};
type Processing = { cache_hits: number; cache_misses: number; model_requests: number; model_fallbacks: number; model_warmup_failures?: number };

export function describeBasis(action: Pick<PlanAction, "explanation_basis" | "model" | "model_note">) {
  if (action.explanation_basis !== "filename-and-size") return { source: "Local extension rule", note: null as string | null };
  return {
    source: `Category suggested by ${action.model ?? "a local model"} from the file name and size only (file contents were not read)`,
    note: action.model_note ? `Model's note, unverified: “${action.model_note}”` : null,
  };
}

export function nameSourceLabel(source: PlanAction["name_source"]) {
  if (source === "user") return "edited by you";
  if (source === "model-suggestion") return "the model's suggestion, applied by you";
  return "original file name";
}

export function scanSummary(count: number, truncated: boolean, processing?: Processing) {
  const base = truncated ? `${count} files shown — scan incomplete` : `${count} files discovered`;
  if (!processing?.model_fallbacks) return base;
  const why = processing.model_warmup_failures ? "the local model did not load in time" : "the local model was unavailable, invalid or over its per-scan limit";
  return `${base}. ${processing.model_fallbacks} file(s) used the built-in rules because ${why}.`;
}
type SkippedFolder = { path: string; reason: string };
type Completeness = {
  scan_root: string; files_scanned: number; limit: number; truncated: boolean; complete: boolean;
  skipped_protected?: SkippedFolder[]; skipped_protected_total?: number; skipped_links?: number; skipped_unreadable?: number;
};

export function incompleteScanMessage(info: Completeness | null) {
  if (!info?.truncated) return "";
  return `Incomplete scan: only the first ${info.files_scanned.toLocaleString()} files were examined because LazyDesk stops at ${info.limit.toLocaleString()} files. More files exist in ${info.scan_root} that are not shown, planned or checked for duplicates.`;
}

export function skippedFoldersMessage(info: Completeness | null) {
  const total = info?.skipped_protected_total ?? 0;
  if (!info || total === 0) return "";
  const shown = (info.skipped_protected ?? []).map((item) => `${item.path} (${item.reason})`).join(", ");
  const more = total > (info.skipped_protected?.length ?? 0) ? `, and ${total - (info.skipped_protected?.length ?? 0)} more` : "";
  return `${total} protected folder${total === 1 ? " was" : "s were"} skipped and never read: ${shown}${more}. Files inside them are not listed, planned or checked for duplicates.`;
}

function ScanWarning({ info }: { info: Completeness | null }) {
  const message = incompleteScanMessage(info);
  const skipped = skippedFoldersMessage(info);
  return (
    <>
      {message ? <div className="alert error" role="alert"><AlertTriangle size={17} /><span>{message}</span></div> : null}
      {skipped ? <div className="alert" role="status"><ShieldCheck size={17} /><span>{skipped}</span></div> : null}
    </>
  );
}

const navItems: { id: View; label: string; icon: typeof LayoutDashboard }[] = [
  { id: "workspace", label: "Workspace", icon: LayoutDashboard },
  { id: "files", label: "Files", icon: FolderOpen },
  { id: "review", label: "Review queue", icon: ClipboardCheck },
  { id: "duplicates", label: "Duplicates", icon: Copy },
  { id: "activity", label: "Activity", icon: Activity },
  { id: "performance", label: "Performance", icon: BarChart3 },
  { id: "settings", label: "Settings", icon: Settings },
];

export function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function fileIcon(extension: string) {
  if (["jpg", "jpeg", "png", "gif", "webp", "svg"].includes(extension)) return FileImage;
  if (["zip", "tar", "gz", "rar"].includes(extension)) return FileArchive;
  if (["ts", "tsx", "js", "jsx", "py", "json", "css", "html"].includes(extension)) return FileCode2;
  if (["txt", "md", "doc", "pdf"].includes(extension)) return FileText;
  return File;
}

// The UI and backend must agree on the port (LAZYDESK_PORT). An unsupported value is shown, and no request is ever sent.
const API_CONFIG = resolveApiConfig(typeof __LAZYDESK_PORT__ === "string" ? __LAZYDESK_PORT__ : undefined);
const client = API_CONFIG.ok ? createApiClient(API_CONFIG.base) : null;
const api = <T,>(path: string, options?: RequestInit): Promise<T> =>
  client ? client.request<T>(path, options) : Promise.reject(new ApiError(API_CONFIG.ok ? "" : API_CONFIG.message));

function ConfirmDialog({ title, children, confirmLabel, busy, error, onConfirm, onCancel }: {
  title: string; children: ReactNode; confirmLabel: string; busy: boolean; error: string; onConfirm: () => void; onCancel: () => void;
}) {
  // Cancel is focused first, so pressing Enter or Space right away cancels instead of confirming. Escape also cancels.
  return (
    <div className="dialog-backdrop" onKeyDown={(event) => { if (event.key === "Escape") onCancel(); }}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title">
        <h3 id="dialog-title">{title}</h3>
        <div className="dialog-body">{children}</div>
        {error && <div className="alert inline" role="alert">{error}</div>}
        <div className="dialog-actions">
          <button className="button secondary" autoFocus onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="button primary" onClick={onConfirm} disabled={busy}>{busy ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [view, setView] = useState<View>("workspace");
  const [path, setPath] = useState("");
  const [activePath, setActivePath] = useState("");
  const [files, setFiles] = useState<FileItem[]>([]);
  const [completeness, setCompleteness] = useState<Completeness | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [benchmark, setBenchmark] = useState<Benchmark | null>(null);
  const [history, setHistory] = useState<Benchmark[]>([]);
  const [activity, setActivity] = useState<{ id: number; kind: string; message: string; created_at: string }[]>([]);
  const [ollama, setOllama] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [modelSource, setModelSource] = useState<string>("none");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [undoTarget, setUndoTarget] = useState<RunSummary | null>(null);
  const [undoBusy, setUndoBusy] = useState(false);
  const [undoError, setUndoError] = useState("");

  const refreshRuns = () => api<{ items: RunSummary[] }>("/runs").then((result) => setRuns(result.items)).catch(() => undefined);
  const confirmUndo = async () => {
    if (!undoTarget) return;
    setUndoBusy(true); setUndoError("");
    try {
      const result = await api<{ undone: string[]; removed_folders: string[]; kept_folders: string[] }>(`/runs/${undoTarget.id}/undo`, { method: "POST" });
      setToast(`Restored ${result.undone.length} file(s)${result.kept_folders.length ? `; ${result.kept_folders.length} folder(s) kept because they are not empty` : ""}. Undo is best-effort.`);
      if (plan && plan.id === undoTarget.plan_id) api<Plan>(`/plans/${plan.id}`).then(setPlan).catch(() => undefined);
      setUndoTarget(null);
    } catch (cause) { setUndoError(cause instanceof Error ? cause.message : "Undo failed."); }
    finally { setUndoBusy(false); refreshRuns(); }
  };

  const loadWorkspace = async (nextPath = activePath) => {
    if (!nextPath) return;
    setLoading(true); setError("");
    try {
      const result = await api<{ path: string; items: FileItem[]; completeness: Completeness; processing?: Processing }>(`/scan`, { method: "POST", body: JSON.stringify({ path: nextPath }) });
      setFiles(result.items); setActivePath(result.path); setSelected([]); setCompleteness(result.completeness);
      setToast(scanSummary(result.items.length, result.completeness.truncated, result.processing));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to scan this folder."); }
    finally { setLoading(false); }
  };

  useEffect(() => {
    api<{ ollama: { available: boolean; models: string[]; selected_model: string | null; model_source?: string } }>("/status").then((result) => { setOllama(result.ollama.available); setModels(result.ollama.models); setSelectedModel(result.ollama.selected_model); setModelSource(result.ollama.model_source ?? "none"); }).catch(() => setOllama(false));
    api<{ items: Benchmark[] }>("/benchmarks").then((result) => setHistory(result.items)).catch(() => undefined);
    api<{ items: typeof activity }>("/activity").then((result) => setActivity(result.items)).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (view === "activity") { api<{ items: typeof activity }>("/activity").then((result) => setActivity(result.items)).catch(() => undefined); refreshRuns(); }
  }, [view]);

  const visibleFiles = useMemo(() => files.filter((file) => file.name.toLowerCase().includes(search.toLowerCase())), [files, search]);
  const selectedFiles = files.filter((file) => selected.includes(file.id));

  const createPlan = async () => {
    setLoading(true); setError("");
    try {
      const result = await api<Plan>("/plans", { method: "POST", body: JSON.stringify({ workspace_root: activePath, files: selectedFiles.map((file) => file.path) }) });
      setPlan(result); setView("review"); setToast("Review plan created; nothing has been changed.");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to create a review plan."); }
    finally { setLoading(false); }
  };

  const runBenchmark = async (synthetic = false) => {
    if (!synthetic && !activePath) return;
    setLoading(true); setError("");
    try {
      const body = synthetic ? { synthetic_files: 200 } : { path: activePath };
      const result = await api<Benchmark>("/benchmarks", { method: "POST", body: JSON.stringify(body) });
      setBenchmark(result); setHistory((current) => [result, ...current]); setToast(synthetic ? "Synthetic benchmark completed (temporary files, temporary cache)." : "Benchmark completed from actual scan data.");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to run benchmark."); }
    finally { setLoading(false); }
  };

  const toggleSelection = (id: string) => setSelected((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  const selectAll = (ids = visibleFiles.map((file) => file.id)) => setSelected((current) => ids.every((id) => current.includes(id)) ? current.filter((id) => !ids.includes(id)) : Array.from(new Set([...current, ...ids])));

  return (
    <div className="app-shell">
      <aside className="rail" aria-label="Primary navigation">
        <div className="brand-mark"><Sparkles size={18} strokeWidth={2.4} /></div>
        <div className="rail-label">LAZYDESK</div>
        <nav>
          {navItems.map(({ id, label, icon: Icon }) => (
            <button key={id} className={`nav-item ${view === id ? "active" : ""}`} onClick={() => setView(id)} title={label} aria-label={label}>
              <Icon size={18} /><span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="rail-bottom"><button className="nav-item" title="Collapse navigation" aria-label="Collapse navigation"><Menu size={18} /></button></div>
      </aside>
      <main className="main-column">
        <header className="topbar">
          <div><p className="eyebrow">LOCAL WORKSPACE</p><h1>LazyDesk AI Studio</h1></div>
          <div className="topbar-actions">
            <span className={`status-pill ${ollama ? "online" : ""}`}><span className="status-dot" />{ollama ? "Ollama connected" : "Ollama unavailable"}</span>
            <button className="icon-button" onClick={() => loadWorkspace()} title="Refresh workspace" aria-label="Refresh workspace"><RefreshCw size={17} /></button>
          </div>
        </header>
        {!API_CONFIG.ok && <div className="alert error" role="alert"><AlertTriangle size={17} /><span>{API_CONFIG.message}</span></div>}
        {error && <div className="alert error"><AlertTriangle size={17} /><span>{error}</span><button onClick={() => setError("")} aria-label="Dismiss error"><X size={15} /></button></div>}
        <ScanWarning info={completeness} />
        {undoTarget && (
          <ConfirmDialog title="Undo this Apply?" confirmLabel="Move files back" busy={undoBusy} error={undoError} onConfirm={confirmUndo} onCancel={() => setUndoTarget(null)}>
            <p>LazyDesk will try to move the {undoTarget.counts.moved ?? 0} file(s) from this run back to where they were, in reverse order, and remove folders it created only if they are empty.</p>
            <p><strong>Undo is best-effort recovery, not a guarantee.</strong> If any file changed, disappeared, or its original path is now occupied, LazyDesk refuses and changes nothing.</p>
          </ConfirmDialog>
        )}
        {toast && <div className="toast"><Check size={16} />{toast}<button onClick={() => setToast("")} aria-label="Dismiss message"><X size={14} /></button></div>}
        {view === "workspace" && <Workspace path={path} setPath={setPath} activePath={activePath} files={files} selected={selected} loading={loading} onScan={() => loadWorkspace(path)} onOpen={() => setView("files")} onReview={createPlan} onBenchmark={() => runBenchmark()} ollama={ollama} />}
        {view === "files" && <FilesView activePath={activePath} files={visibleFiles} selected={selected} search={search} setSearch={setSearch} loading={loading} toggleSelection={toggleSelection} selectAll={selectAll} onReview={createPlan} />}
        {view === "review" && <ReviewView plan={plan} onBack={() => setView("files")} onPlanChange={setPlan} />}
        {view === "duplicates" && <DuplicatesView activePath={activePath} />}
        {view === "review" && plan && <p className="muted" style={{ padding: "0 24px" }}>Workspace boundary: {activePath}</p>}
        {view === "activity" && <ActivityView activity={activity} runs={runs} onUndo={(run) => { setUndoError(""); setUndoTarget(run); }} />}
        {view === "performance" && <PerformanceView benchmark={benchmark} history={history} onRun={runBenchmark} activePath={activePath} loading={loading} />}
        {view === "settings" && <SettingsView ollama={ollama} models={models} selectedModel={selectedModel} modelSource={modelSource} onModelChange={(model) => { setSelectedModel(model); setModelSource(model ? "setting" : "none"); }} />}
        <footer className="statusbar"><span><ShieldCheck size={14} /> Local-only mode</span><span>{activePath || "No folder selected"}</span><span>{files.length ? `${files.length} files${completeness?.truncated ? " (incomplete scan)" : ""}` : "Ready"}</span></footer>
      </main>
    </div>
  );
}

function Workspace({ path, setPath, activePath, files, selected, loading, onScan, onOpen, onReview, onBenchmark, ollama }: { path: string; setPath: (value: string) => void; activePath: string; files: FileItem[]; selected: string[]; loading: boolean; onScan: () => void; onOpen: () => void; onReview: () => void; onBenchmark: () => void; ollama: boolean }) {
  return <section className="content">
    <div className="page-heading"><div><p className="eyebrow">WORKSPACE</p><h2>Organize with confidence.</h2><p className="muted">Inspect a local folder, review every proposal, then decide what changes to apply.</p></div><div className="heading-actions"><button className="button secondary" onClick={onBenchmark} disabled={!activePath || loading}><BarChart3 size={16} />Run benchmark</button><button className="button primary" onClick={onOpen} disabled={!activePath}><FolderOpen size={16} />Open files</button></div></div>
    <div className="scan-panel"><div className="scan-icon"><HardDrive size={20} /></div><div className="scan-copy"><strong>Select a local folder to begin</strong><span>Only this directory is scanned. No files are uploaded or changed during discovery.</span><div className="path-input"><Folder size={16} /><input value={path} onChange={(event) => setPath(event.target.value)} placeholder="C:\Users\you\Downloads" aria-label="Local folder path" /><button onClick={onScan} disabled={!path || loading}>{loading ? <LoaderCircle className="spin" size={16} /> : "Scan folder"}</button></div></div></div>
    <div className="section-header"><div><p className="eyebrow">CURRENT RUN</p><h3>{activePath ? activePath : "No folder scanned yet"}</h3></div><span className="muted">{files.length ? `${files.length} files discovered` : "Waiting for a folder"}</span></div>
    <div className="metric-row"><Metric label="Files discovered" value={files.length ? files.length.toString() : "—"} icon={<FolderOpen size={16} />} /><Metric label="Selected for review" value={selected.length ? selected.length.toString() : "—"} icon={<ClipboardCheck size={16} />} /><Metric label="Model requests" value={ollama ? "Ready" : "0"} icon={<Sparkles size={16} />} /></div>
    <div className="empty-workspace">{files.length ? <><FileTablePreview files={files.slice(0, 6)} /><button className="button secondary" onClick={onReview} disabled={!selected.length}>Create review plan <ChevronRight size={15} /></button></> : <><Archive size={28} /><strong>Your workspace is ready</strong><span>Scan a directory to see actual files here. The assistant stays out of the way until you ask it to propose changes.</span></>}</div>
  </section>;
}

function Metric({ label, value, icon }: { label: string; value: string; icon: ReactNode }) { return <div className="metric"><span className="metric-icon">{icon}</span><div><span>{label}</span><strong>{value}</strong></div></div>; }
function FileTablePreview({ files }: { files: FileItem[] }) { return <div className="mini-table"><div className="table-head"><span>Name</span><span>Type</span><span>Size</span></div>{files.map((item) => { const Icon = fileIcon(item.extension); return <div className="table-row" key={item.id}><span><Icon size={16} />{item.name}</span><span>.{item.extension}</span><span>{formatBytes(item.size)}</span></div>; })}</div>; }

function FilesView({ activePath, files, selected, search, setSearch, loading, toggleSelection, selectAll, onReview }: { activePath: string; files: FileItem[]; selected: string[]; search: string; setSearch: (value: string) => void; loading: boolean; toggleSelection: (id: string) => void; selectAll: (ids?: string[]) => void; onReview: () => void }) {
  const [extensionFilter, setExtensionFilter] = useState("all");
  const [descending, setDescending] = useState(false);
  const extensions = useMemo(() => Array.from(new Set(files.map((file) => file.extension))).sort(), [files]);
  const displayFiles = useMemo(() => files.filter((file) => extensionFilter === "all" || file.extension === extensionFilter).sort((left, right) => {
    const comparison = left.name.localeCompare(right.name);
    return descending ? -comparison : comparison;
  }), [files, extensionFilter, descending]);
  return <section className="content"><div className="page-heading compact"><div><p className="eyebrow">FILES</p><h2>File workspace</h2><div className="breadcrumbs"><FolderOpen size={15} />{activePath || "Select a folder from Workspace"}</div></div><button className="button primary" onClick={onReview} disabled={!selected.length}>Review {selected.length || ""} selected <ChevronRight size={15} /></button></div>
    <div className="toolbar"><div className="search-box"><Search size={16} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search files" aria-label="Search files" /></div><select className="filter-button" value={extensionFilter} onChange={(event) => setExtensionFilter(event.target.value)} aria-label="Filter by extension"><option value="all">All types</option>{extensions.map((extension) => <option key={extension} value={extension}>.{extension}</option>)}</select><button className="filter-button" onClick={() => setDescending((current) => !current)}><ArrowDownUp size={15} />Name {descending ? "Z–A" : "A–Z"}</button></div>
    <div className="file-table"><div className="file-table-head"><label><input type="checkbox" checked={displayFiles.length > 0 && displayFiles.every((file) => selected.includes(file.id))} onChange={() => selectAll(displayFiles.map((file) => file.id))} aria-label="Select visible files" />Name</label><span>Type</span><span>Modified</span><span>Size</span></div>{loading ? <div className="table-empty"><LoaderCircle className="spin" />Scanning local folder…</div> : displayFiles.length ? displayFiles.map((item) => { const Icon = fileIcon(item.extension); return <div className={`file-row ${selected.includes(item.id) ? "selected" : ""}`} key={item.id}><label><input type="checkbox" checked={selected.includes(item.id)} onChange={() => toggleSelection(item.id)} aria-label={`Select ${item.name}`} /><span className="file-name"><Icon size={17} />{item.name}</span></label><span className="type-badge" title={item.classification_reason ?? ""}>.{item.extension}{item.category ? ` · ${item.category}` : ""}{item.classification_source?.endsWith("ollama") ? " · AI" : ""}</span><span>{new Date(item.modified_at).toLocaleDateString()}</span><span>{formatBytes(item.size)}</span></div>; }) : <div className="table-empty"><FolderOpen size={24} /><strong>No files found</strong><span>Try another folder or adjust your search.</span></div>}</div>
  </section>;
}

function ReviewView({ plan, onBack, onPlanChange }: { plan: Plan | null; onBack: () => void; onPlanChange: (plan: Plan) => void }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const approvable = (action: PlanAction) => action.status === "pending" && action.validation !== "blocked";
  const approveActions = async (ids: string[]) => {
    if (!plan || !ids.length) return;
    setBusy(true); setMessage("");
    try {
      const revisions = Object.fromEntries(plan.actions.filter((action) => ids.includes(action.id)).map((action) => [action.id, action.revision ?? 0]));
      const updated = await api<Plan>(`/plans/${plan.id}/approve`, { method: "POST", body: JSON.stringify({ action_ids: ids, revisions }) });
      onPlanChange(updated);
      setMessage(`${ids.length} proposal(s) approved. Nothing has been moved yet.`);
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : "Approval failed."); }
    finally { setBusy(false); }
  };
  const [preview, setPreview] = useState<Preview | null>(null);
  const [dialogError, setDialogError] = useState("");
  const refreshPlan = () => plan ? api<Plan>(`/plans/${plan.id}`).then(onPlanChange).catch(() => undefined) : undefined;
  const applyApproved = async () => {  // step 1: ask the backend for the exact moves; nothing is moved yet
    if (!plan) return;
    const ids = plan.actions.filter((action) => action.status === "approved").map((action) => action.id);
    if (!ids.length) return;
    setBusy(true); setMessage("");
    try {
      setDialogError("");
      setPreview(await api<Preview>(`/plans/${plan.id}/preview`, { method: "POST", body: JSON.stringify({ action_ids: ids }) }));
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : "Unable to prepare the Apply preview."); }
    finally { setBusy(false); }
  };
  const confirmApply = async () => {  // step 2: only the confirmed preview can be applied
    if (!plan || !preview) return;
    setBusy(true); setDialogError("");
    try {
      const result = await api<{ count: number; run_id: string }>("/execute", { method: "POST", body: JSON.stringify({
        plan_id: plan.id, action_ids: preview.moves.map((move) => move.action_id), confirmation_token: preview.confirmation_token }) });
      setPreview(null);
      setMessage(`${result.count} approved operation(s) applied (run ${result.run_id}). You can undo this from Activity — best-effort, not guaranteed.`);
      refreshPlan();
    } catch (cause) {
      setDialogError(cause instanceof Error ? cause.message : "Execution failed.");  // the dialog stays open with the explanation; the token is spent
      refreshPlan();
    } finally { setBusy(false); }
  };
  const reject = async (action: PlanAction) => {
    if (!plan) return;
    try {
      const updated = await api<Plan>(`/plans/${plan.id}/reject`, { method: "POST", body: JSON.stringify({ action_ids: [action.id] }) });
      onPlanChange(updated);
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : "Unable to reject suggestion."); }
  };
  const saveName = async (action: PlanAction) => {
    if (!plan) return;
    const proposedName = drafts[action.id] ?? action.proposed_name;
    if (proposedName === action.proposed_name) return;
    try {
      const updated = await api<Plan>(`/plans/${plan.id}/actions/${action.id}`, { method: "PATCH", body: JSON.stringify({ proposed_name: proposedName }) });
      onPlanChange(updated);
      const result = updated.actions.find((item) => item.id === action.id);
      setMessage(result?.validation === "blocked" ? `Name not accepted: ${result.reason}` : "Name updated. Approve this proposal again before applying.");
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : "Unable to validate destination."); }
  };
  const useSuggestion = async (action: PlanAction) => {
    if (!plan) return;
    try {
      const updated = await api<Plan>(`/plans/${plan.id}/actions/${action.id}`, { method: "PATCH", body: JSON.stringify({ use_suggestion: true }) });
      onPlanChange(updated);
      setDrafts((current) => { const next = { ...current }; delete next[action.id]; return next; });
      setMessage("Suggested name applied. It is not approved yet; approve this proposal again before applying.");
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : "Unable to apply the suggested name."); }
  };
  return <section className="content"><div className="page-heading compact"><div><p className="eyebrow">REVIEW QUEUE</p><h2>Proposed changes</h2><p className="muted">Review each proposal, approve the ones you want, then apply them. Nothing moves until you apply approved proposals.</p></div><button className="button secondary" onClick={onBack}>Back to files</button></div>
    {!plan ? <EmptyState icon={<ClipboardCheck size={26} />} title="No plan to review" text="Select files from the Files workspace to create a local organization plan." /> : <><div className="review-summary"><div><strong>{plan.actions.length}</strong><span>proposals</span></div><div><strong>{plan.actions.filter((action) => action.validation === "safe").length}</strong><span>safe</span></div><div><strong>{plan.actions.filter((action) => action.validation === "blocked").length}</strong><span>blocked</span></div><span className="review-note"><ShieldCheck size={15} />Every destination is validated on the backend.</span><button className="button secondary" onClick={() => approveActions(plan.actions.filter(approvable).map((action) => action.id))} disabled={busy || !plan.actions.some(approvable)}>Approve all valid</button><button className="button primary" onClick={applyApproved} disabled={busy || !plan.actions.some((action) => action.status === "approved")}>{busy ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}Apply {plan.actions.filter((action) => action.status === "approved").length} approved</button></div>{message && <div className="alert inline">{message}</div>}{preview && (
      <ConfirmDialog title={`Move ${preview.count} file${preview.count === 1 ? "" : "s"}?`} confirmLabel={`Move ${preview.count} file${preview.count === 1 ? "" : "s"}`} busy={busy} error={dialogError} onConfirm={confirmApply} onCancel={() => { setPreview(null); setDialogError(""); }}>
        <p>These exact moves will happen, in this order:</p>
        <ol className="move-list">{preview.moves.map((move) => <li key={move.action_id}>{moveLine(move)}</li>)}</ol>
        {preview.creates_folders.length > 0 && <p>New folders: {preview.creates_folders.join(", ")}</p>}
        <p className="muted">{expiryNote(preview.expires_in_seconds)}</p>
        <p className="muted">{preview.undo_note}</p>
      </ConfirmDialog>
    )}<div className="review-list">{plan.actions.map((action) => <div className="review-card" key={action.id}><div className="review-icon">{action.validation === "blocked" ? <X size={17} /> : action.validation === "warning" ? <AlertTriangle size={17} /> : <Check size={17} />}</div><div className="review-main"><div className="review-title"><input className="review-name-input" value={drafts[action.id] ?? action.proposed_name} onChange={(event) => setDrafts((current) => ({ ...current, [action.id]: event.target.value }))} onBlur={() => saveName(action)} onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }} aria-label={`Proposed name for ${action.proposed_name}`} /><span className={`validation ${action.status === "rejected" ? "blocked" : action.validation}`}>{action.status === "rejected" ? "rejected" : action.status === "approved" ? "approved" : action.status === "executed" ? "applied" : action.validation}</span></div><div className="before-after"><span>{action.current_location}</span><ChevronRight size={15} /><span>{action.proposed_destination}</span></div><p>{action.reason}</p><small>Suggestion source: {describeBasis(action).source}</small>{describeBasis(action).note && <small className="model-note">{describeBasis(action).note}</small>}{action.suggested_name && action.name_source !== "model-suggestion" && <div className="suggestion-row"><small>The model also suggests the name <strong>{action.suggested_name}</strong>. Nothing is renamed unless you choose to.</small><button className="button secondary" onClick={() => useSuggestion(action)} disabled={action.status === "executed" || action.status === "rejected"}>Use suggested name</button></div>}{action.name_source && action.name_source !== "original" && <small>Name: {nameSourceLabel(action.name_source)}</small>}{isLegacyPlan(action) && <small className="model-note">Created by an older version. It is re-checked with the current safety rules and may be refused (for example, without a content fingerprint) until you create a new plan.</small>}</div><button className="icon-button" onClick={() => approveActions([action.id])} disabled={busy || !approvable(action)} title="Approve suggestion" aria-label={`Approve ${action.proposed_name}`}><Check size={16} /></button><button className="icon-button" onClick={() => reject(action)} disabled={action.status === "rejected" || action.status === "executed"} title="Reject suggestion" aria-label={`Reject ${action.proposed_name}`}><X size={16} /></button></div>)}</div></>}
  </section>;
}

function ActivityView({ activity, runs, onUndo }: { activity: { id: number; kind: string; message: string; created_at: string }[]; runs: RunSummary[]; onUndo: (run: RunSummary) => void }) { return <section className="content"><div className="page-heading compact"><div><p className="eyebrow">ACTIVITY</p><h2>Recent activity</h2><p className="muted">A local record of scans, plans, and benchmark runs.</p></div></div>{runs.length > 0 && <div className="runs-panel"><h3>Apply history</h3><p className="muted">Undo is best-effort recovery, not a guarantee: it refuses, and changes nothing, if a file is no longer exactly as LazyDesk left it.</p>{runs.map((run) => <div className="activity-item" key={run.id}><span className="activity-icon"><Activity size={16} /></span><div><strong>{statusLabel(run.status)} · {countsText(run)}</strong><span>{run.summary} · {new Date(run.created_at).toLocaleString()}{run.needs_attention ? " · needs a manual check; LazyDesk will not move anything for you" : ""}</span></div><button className="button secondary" onClick={() => onUndo(run)} disabled={!run.can_undo}>Undo</button></div>)}</div>}{activity.length ? <div className="activity-list">{activity.map((item) => <div className="activity-item" key={item.id}><span className="activity-icon"><Activity size={16} /></span><div><strong>{item.message}</strong><span>{item.kind} · {new Date(item.created_at).toLocaleString()}</span></div></div>)}</div> : <EmptyState icon={<Clock3 size={26} />} title="No activity yet" text="Your local actions will appear here as you work." />}</section>; }
function PerformanceView({ benchmark, history, onRun, activePath, loading }: { benchmark: Benchmark | null; history: Benchmark[]; onRun: (synthetic?: boolean) => void; activePath: string; loading: boolean }) {
  const phase = (label: string, value?: { processed: number; cache_hits: number; duration_ms: number }) => value ? <div className="history-row" key={label}><span>{label}</span><span>{value.duration_ms} ms (median)</span><span>{value.processed} classified</span><span>{value.cache_hits} cache hits</span></div> : null;
  return <section className="content"><div className="page-heading compact"><div><p className="eyebrow">PERFORMANCE</p><h2>Optimization inspection</h2><p className="muted">Measured results only. Timings use temporary files and a temporary cache, so they never change your data.</p></div><div className="heading-actions"><button className="button secondary" onClick={() => onRun(true)} disabled={loading}><Play size={15} />Run synthetic (200 files)</button><button className="button primary" onClick={() => onRun(false)} disabled={!activePath || loading}><Play size={15} />Run on active folder</button></div></div>
    {benchmark ? <>
      <div className="benchmark-banner"><BarChart3 size={19} /><div><strong>Current run · {benchmark.mode}</strong><span>{benchmark.path} · {new Date(benchmark.created_at).toLocaleString()}</span></div></div>
      <div className="metric-grid">
        <Metric label="Files discovered" value={String(benchmark.files_discovered)} icon={<FolderOpen size={16} />} />
        <Metric label="Warm-cache hits" value={`${benchmark.warm.cache_hits} / ${benchmark.warm.files}`} icon={<RefreshCw size={16} />} />
        <Metric label="Model requests" value={String(benchmark.local_model_requests)} icon={<Sparkles size={16} />} />
        <Metric label="No cache → warm cache" value={`${benchmark.baseline_duration_ms} → ${benchmark.optimized_duration_ms} ms`} icon={<Clock3 size={16} />} />
        <Metric label="Warm vs no cache" value={benchmark.speedup_warm_vs_baseline ? `${benchmark.speedup_warm_vs_baseline}×` : "—"} icon={<BarChart3 size={16} />} />
        <Metric label="Results identical" value={benchmark.results_match === false ? "No" : "Yes"} icon={<ShieldCheck size={16} />} />
      </div>
      <div className="history"><div className="section-header"><div><p className="eyebrow">PHASES</p><h3>Same files, three ways</h3></div></div>
        {phase("No cache (rules only)", benchmark.cold)}{phase("Cold cache (first pass)", benchmark.optimized_cold)}{phase("Warm cache (second pass)", benchmark.warm)}</div>
      {benchmark.methodology && <p className="muted" style={{ marginTop: 12 }}>{benchmark.methodology}</p>}
    </> : <EmptyState icon={<BarChart3 size={26} />} title="No benchmark history" text="Run a synthetic benchmark, or one on the active folder, to measure actual classification behavior." />}
    {history.length > 0 && <div className="history"><div className="section-header"><div><p className="eyebrow">HISTORY</p><h3>Previous runs</h3></div></div>{history.map((item) => <div className="history-row" key={item.id}><span>{new Date(item.created_at).toLocaleString()}</span><span>{item.files_discovered} files</span><span>{item.baseline_duration_ms} → {item.optimized_duration_ms} ms</span><span>{item.path}</span></div>)}</div>}</section>;
}
function DuplicatesView({ activePath }: { activePath: string }) {
  const [groups, setGroups] = useState<{ size: number; fingerprint: string; files: string[] }[]>([]);
  const [dupInfo, setDupInfo] = useState<Completeness | null>(null);
  const [cloudSkipped, setCloudSkipped] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const scan = async () => {
    if (!activePath) return;
    setLoading(true); setError("");
    try { const result = await api<{ groups: typeof groups; completeness: Completeness; skipped_cloud_only?: number }>("/duplicates", { method: "POST", body: JSON.stringify({ path: activePath }) }); setGroups(result.groups); setDupInfo(result.completeness); setCloudSkipped(result.skipped_cloud_only ?? 0); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to scan for duplicates."); }
    finally { setLoading(false); }
  };
  return <section className="content"><div className="page-heading compact"><div><p className="eyebrow">DUPLICATES</p><h2>Exact duplicate groups</h2><p className="muted">Only identical SHA-256 content fingerprints are grouped. LazyDesk never deletes duplicates automatically.</p></div><button className="button primary" onClick={scan} disabled={!activePath || loading}>{loading ? <LoaderCircle className="spin" size={15} /> : <Copy size={15} />}Scan duplicates</button></div>{error && <div className="alert inline">{error}</div>}<ScanWarning info={dupInfo} />{cloudSkipped > 0 && <div className="alert inline" role="status">{cloudSkipped} cloud-only file(s) were not compared because reading them would download them.</div>}{groups.length ? <div className="duplicate-list">{groups.map((group) => <div className="duplicate-card" key={group.fingerprint}><div className="duplicate-heading"><strong>{group.files.length} exact matches</strong><span>{formatBytes(group.size)} each · SHA-256 {group.fingerprint.slice(0, 16)}…</span></div>{group.files.map((file) => <div className="duplicate-file" key={file}><File size={15} />{file}</div>)}</div>)}</div> : <EmptyState icon={<Copy size={26} />} title={activePath ? (dupInfo ? "No exact duplicates found" : "No duplicate scan yet") : "Select a workspace first"} text={activePath ? (dupInfo ? "No two scanned files have identical content." : "Run an exact-content duplicate scan when you are ready.") : "Scan a folder from Workspace before checking duplicates."} />}</section>;
}
export function describeModelChoice(model: string | null, source: string, installed: boolean) {
  if (!model) return "No model selected: classification uses the built-in rules only.";
  const where = source === "default" ? "the built-in default" : source === "environment" ? "the OLLAMA_MODEL environment variable" : "your saved choice";
  return installed ? `Using ${model} (${where}) for files with no extension rule.` : `${model} (${where}) is not installed, so the built-in rules are used.`;
}
function SettingsView({ ollama, models, selectedModel, modelSource, onModelChange }: { ollama: boolean; models: string[]; selectedModel: string | null; modelSource: string; onModelChange: (model: string | null) => void }) {
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [savedChoice, setSavedChoice] = useState<"model" | "none" | null>(null);
  const loadSaved = () => api<{ saved_choice: "model" | "none" | null }>("/settings").then((result) => setSavedChoice(result.saved_choice)).catch(() => undefined);
  useEffect(() => { loadSaved(); }, []);
  const fallback = fallbackControl(savedChoice, saving);
  const saveModel = async (model: string) => {
    setSaving(true); setSaveError("");
    try { const result = await api<{ model: string | null }>("/settings", { method: "PATCH", body: JSON.stringify({ model: model || null }) }); onModelChange(result.model); await loadSaved(); }
    catch (cause) { setSaveError(cause instanceof Error ? cause.message : "Unable to save the model selection."); }
    finally { setSaving(false); }
  };
  return <section className="content"><div className="page-heading compact"><div><p className="eyebrow">SETTINGS</p><h2>Local preferences</h2><p className="muted">Keep control of where LazyDesk can look and which local model it can use.</p></div></div>{saveError && <div className="alert inline" role="alert">{saveError}</div>}<div className="settings-list"><div className="setting-row"><div><strong>Ollama connection</strong><span>Optional local inference for richer classification suggestions.</span></div><span className={`status-pill ${ollama ? "online" : ""}`}><span className="status-dot" />{ollama ? "Connected" : "Unavailable"}</span></div><div className="setting-row"><div><strong>Classification model</strong><span>Only installed local models appear here; LazyDesk never downloads one. {describeModelChoice(selectedModel, modelSource, !!selectedModel && models.includes(selectedModel))}</span></div><select value={selectedModel && models.includes(selectedModel) ? selectedModel : ""} onChange={(event) => saveModel(event.target.value)} disabled={saving || !models.length} aria-label="Ollama classification model"><option value="">Deterministic fallback</option>{models.map((model) => <option key={model} value={model}>{model}</option>)}</select><button className="button secondary" onClick={() => saveModel("")} disabled={fallback.disabled}>{fallback.label}</button></div><div className="setting-row"><div><strong>Filesystem safety</strong><span>Backend validates every path and blocks destinations outside the active workspace.</span></div><ShieldCheck size={20} className="green-icon" /></div><div className="setting-row"><div><strong>Storage</strong><span>Activity, benchmark, plan, and cache history are stored in local SQLite.</span></div><HardDrive size={20} /></div></div></section>;
}
function EmptyState({ icon, title, text }: { icon: ReactNode; title: string; text: string }) { return <div className="empty-state">{icon}<strong>{title}</strong><span>{text}</span></div>; }
