import { useEffect, useMemo, useState } from "react";
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
};
type PlanAction = {
  id: string; source: string; proposed_name: string; current_location: string;
  proposed_destination: string; source_type: "local-rule" | "ollama";
  validation: "safe" | "warning" | "blocked"; reason: string; status: "pending" | "approved" | "rejected" | "executed";
};
type Plan = { id: string; created_at: string; actions: PlanAction[] };
type Benchmark = {
  id: string; created_at: string; files_discovered: number; unchanged_files_skipped: number;
  cache_hits: number; cache_misses: number; local_model_requests: number; optimized_duration_ms: number;
  baseline_duration_ms: number; throughput: number; classification_quality: string; path: string;
  mode: "workspace" | "synthetic"; cold: { files: number; processed: number; cache_hits: number; duration_ms: number };
  warm: { files: number; processed: number; cache_hits: number; duration_ms: number };
};
const API = "http://127.0.0.1:8000/api";

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

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  const payload = await response.json();
  if (!response.ok) {
    const detail = payload.detail;
    throw new Error(typeof detail === "string" ? detail : detail?.message ?? "The local API request failed.");
  }
  return payload as T;
}

export default function App() {
  const [view, setView] = useState<View>("workspace");
  const [path, setPath] = useState("");
  const [activePath, setActivePath] = useState("");
  const [files, setFiles] = useState<FileItem[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [benchmark, setBenchmark] = useState<Benchmark | null>(null);
  const [history, setHistory] = useState<Benchmark[]>([]);
  const [activity, setActivity] = useState<{ id: number; kind: string; message: string; created_at: string }[]>([]);
  const [ollama, setOllama] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");

  const loadWorkspace = async (nextPath = activePath) => {
    if (!nextPath) return;
    setLoading(true); setError("");
    try {
      const result = await api<{ path: string; items: FileItem[] }>(`/scan`, { method: "POST", body: JSON.stringify({ path: nextPath }) });
      setFiles(result.items); setActivePath(result.path); setSelected([]);
      setToast(`${result.items.length} files discovered`);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to scan this folder."); }
    finally { setLoading(false); }
  };

  useEffect(() => {
    api<{ ollama: { available: boolean; models: string[]; selected_model: string | null } }>("/health").then((result) => { setOllama(result.ollama.available); setModels(result.ollama.models); setSelectedModel(result.ollama.selected_model); }).catch(() => setOllama(false));
    api<{ items: Benchmark[] }>("/benchmarks").then((result) => setHistory(result.items)).catch(() => undefined);
    api<{ items: typeof activity }>("/activity").then((result) => setActivity(result.items)).catch(() => undefined);
  }, []);

  const visibleFiles = useMemo(() => files.filter((file) => file.name.toLowerCase().includes(search.toLowerCase())), [files, search]);
  const selectedFiles = files.filter((file) => selected.includes(file.id));

  const createPlan = async () => {
    setLoading(true); setError("");
    try {
      const result = await api<Plan>("/plans", { method: "POST", body: JSON.stringify({ files: selectedFiles.map((file) => file.path) }) });
      setPlan(result); setView("review"); setToast("Review plan created; nothing has been changed.");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to create a review plan."); }
    finally { setLoading(false); }
  };

  const runBenchmark = async () => {
    if (!activePath) return;
    setLoading(true); setError("");
    try {
      const result = await api<Benchmark>("/benchmarks", { method: "POST", body: JSON.stringify({ path: activePath }) });
      setBenchmark(result); setHistory((current) => [result, ...current]); setToast("Benchmark completed from actual scan data.");
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
        {error && <div className="alert error"><AlertTriangle size={17} /><span>{error}</span><button onClick={() => setError("")} aria-label="Dismiss error"><X size={15} /></button></div>}
        {toast && <div className="toast"><Check size={16} />{toast}<button onClick={() => setToast("")} aria-label="Dismiss message"><X size={14} /></button></div>}
        {view === "workspace" && <Workspace path={path} setPath={setPath} activePath={activePath} files={files} selected={selected} loading={loading} onScan={() => loadWorkspace(path)} onOpen={() => setView("files")} onReview={createPlan} onBenchmark={runBenchmark} ollama={ollama} />}
        {view === "files" && <FilesView activePath={activePath} files={visibleFiles} selected={selected} search={search} setSearch={setSearch} loading={loading} toggleSelection={toggleSelection} selectAll={selectAll} onReview={createPlan} />}
        {view === "review" && <ReviewView plan={plan} onBack={() => setView("files")} onPlanChange={setPlan} />}
        {view === "duplicates" && <DuplicatesView activePath={activePath} />}
        {view === "activity" && <ActivityView activity={activity} />}
        {view === "performance" && <PerformanceView benchmark={benchmark} history={history} onRun={runBenchmark} activePath={activePath} />}
        {view === "settings" && <SettingsView ollama={ollama} models={models} selectedModel={selectedModel} onModelChange={setSelectedModel} />}
        <footer className="statusbar"><span><ShieldCheck size={14} /> Local-only mode</span><span>{activePath || "No folder selected"}</span><span>{files.length ? `${files.length} files` : "Ready"}</span></footer>
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
    <div className="file-table"><div className="file-table-head"><label><input type="checkbox" checked={displayFiles.length > 0 && displayFiles.every((file) => selected.includes(file.id))} onChange={() => selectAll(displayFiles.map((file) => file.id))} aria-label="Select visible files" />Name</label><span>Type</span><span>Modified</span><span>Size</span></div>{loading ? <div className="table-empty"><LoaderCircle className="spin" />Scanning local folder…</div> : displayFiles.length ? displayFiles.map((item) => { const Icon = fileIcon(item.extension); return <div className={`file-row ${selected.includes(item.id) ? "selected" : ""}`} key={item.id}><label><input type="checkbox" checked={selected.includes(item.id)} onChange={() => toggleSelection(item.id)} aria-label={`Select ${item.name}`} /><span className="file-name"><Icon size={17} />{item.name}</span></label><span className="type-badge">.{item.extension}</span><span>{new Date(item.modified_at).toLocaleDateString()}</span><span>{formatBytes(item.size)}</span></div>; }) : <div className="table-empty"><FolderOpen size={24} /><strong>No files found</strong><span>Try another folder or adjust your search.</span></div>}</div>
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
      const updated = await api<Plan>(`/plans/${plan.id}/approve`, { method: "POST", body: JSON.stringify({ action_ids: ids }) });
      onPlanChange(updated);
      setMessage(`${ids.length} proposal(s) approved. Nothing has been moved yet.`);
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : "Approval failed."); }
    finally { setBusy(false); }
  };
  const applyApproved = async () => {
    if (!plan) return;
    const ids = plan.actions.filter((action) => action.status === "approved").map((action) => action.id);
    if (!ids.length) return;
    setBusy(true); setMessage("");
    try {
      const result = await api<{ count: number }>("/execute", { method: "POST", body: JSON.stringify({ plan_id: plan.id, action_ids: ids }) });
      setMessage(`${result.count} approved operation(s) applied.`);
      onPlanChange({ ...plan, actions: plan.actions.map((action) => ids.includes(action.id) ? { ...action, status: "executed" } : action) });
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : "Execution failed."); }
    finally { setBusy(false); }
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
      setMessage("Destination validated by the backend.");
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : "Unable to validate destination."); }
  };
  return <section className="content"><div className="page-heading compact"><div><p className="eyebrow">REVIEW QUEUE</p><h2>Proposed changes</h2><p className="muted">Review each proposal, approve the ones you want, then apply them. Nothing moves until you apply approved proposals.</p></div><button className="button secondary" onClick={onBack}>Back to files</button></div>
    {!plan ? <EmptyState icon={<ClipboardCheck size={26} />} title="No plan to review" text="Select files from the Files workspace to create a local organization plan." /> : <><div className="review-summary"><div><strong>{plan.actions.length}</strong><span>proposals</span></div><div><strong>{plan.actions.filter((action) => action.validation === "safe").length}</strong><span>safe</span></div><div><strong>{plan.actions.filter((action) => action.validation === "blocked").length}</strong><span>blocked</span></div><span className="review-note"><ShieldCheck size={15} />Every destination is validated on the backend.</span><button className="button secondary" onClick={() => approveActions(plan.actions.filter(approvable).map((action) => action.id))} disabled={busy || !plan.actions.some(approvable)}>Approve all valid</button><button className="button primary" onClick={applyApproved} disabled={busy || !plan.actions.some((action) => action.status === "approved")}>{busy ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}Apply {plan.actions.filter((action) => action.status === "approved").length} approved</button></div>{message && <div className="alert inline">{message}</div>}<div className="review-list">{plan.actions.map((action) => <div className="review-card" key={action.id}><div className="review-icon">{action.validation === "blocked" ? <X size={17} /> : action.validation === "warning" ? <AlertTriangle size={17} /> : <Check size={17} />}</div><div className="review-main"><div className="review-title"><input className="review-name-input" value={drafts[action.id] ?? action.proposed_name} onChange={(event) => setDrafts((current) => ({ ...current, [action.id]: event.target.value }))} onBlur={() => saveName(action)} onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }} aria-label={`Proposed name for ${action.proposed_name}`} /><span className={`validation ${action.status === "rejected" ? "blocked" : action.validation}`}>{action.status === "rejected" ? "rejected" : action.status === "approved" ? "approved" : action.status === "executed" ? "applied" : action.validation}</span></div><div className="before-after"><span>{action.current_location}</span><ChevronRight size={15} /><span>{action.proposed_destination}</span></div><p>{action.reason}</p><small>Suggestion source: {action.source_type === "ollama" ? "Ollama" : "Local rule"}</small></div><button className="icon-button" onClick={() => approveActions([action.id])} disabled={busy || !approvable(action)} title="Approve suggestion" aria-label={`Approve ${action.proposed_name}`}><Check size={16} /></button><button className="icon-button" onClick={() => reject(action)} disabled={action.status === "rejected" || action.status === "executed"} title="Reject suggestion" aria-label={`Reject ${action.proposed_name}`}><X size={16} /></button></div>)}</div></>}
  </section>;
}

function ActivityView({ activity }: { activity: { id: number; kind: string; message: string; created_at: string }[] }) { return <section className="content"><div className="page-heading compact"><div><p className="eyebrow">ACTIVITY</p><h2>Recent activity</h2><p className="muted">A local record of scans, plans, and benchmark runs.</p></div></div>{activity.length ? <div className="activity-list">{activity.map((item) => <div className="activity-item" key={item.id}><span className="activity-icon"><Activity size={16} /></span><div><strong>{item.message}</strong><span>{item.kind} · {new Date(item.created_at).toLocaleString()}</span></div></div>)}</div> : <EmptyState icon={<Clock3 size={26} />} title="No activity yet" text="Your local actions will appear here as you work." />}</section>; }
function PerformanceView({ benchmark, history, onRun, activePath }: { benchmark: Benchmark | null; history: Benchmark[]; onRun: () => void; activePath: string }) { return <section className="content"><div className="page-heading compact"><div><p className="eyebrow">PERFORMANCE</p><h2>Optimization inspection</h2><p className="muted">Measured results only. Run a benchmark against the active folder to populate this view.</p></div><button className="button primary" onClick={onRun} disabled={!activePath}><Play size={15} />Run benchmark</button></div>{benchmark ? <><div className="benchmark-banner"><BarChart3 size={19} /><div><strong>Current run · {benchmark.mode}</strong><span>{benchmark.path} · {new Date(benchmark.created_at).toLocaleString()}</span></div></div><div className="metric-grid"><Metric label="Files discovered" value={String(benchmark.files_discovered)} icon={<FolderOpen size={16} />} /><Metric label="Warm-cache hits" value={String(benchmark.warm.cache_hits)} icon={<RefreshCw size={16} />} /><Metric label="Model requests" value={String(benchmark.local_model_requests)} icon={<Sparkles size={16} />} /><Metric label="Cold → warm" value={`${benchmark.baseline_duration_ms} → ${benchmark.optimized_duration_ms} ms`} icon={<Clock3 size={16} />} /><Metric label="Warm throughput" value={`${benchmark.throughput}/s`} icon={<BarChart3 size={16} />} /><Metric label="Quality" value={benchmark.classification_quality} icon={<ShieldCheck size={16} />} /></div></> : <EmptyState icon={<BarChart3 size={26} />} title="No benchmark history" text="Run a benchmark to measure actual scan and classification behavior." />}{history.length > 0 && <div className="history"><div className="section-header"><div><p className="eyebrow">HISTORY</p><h3>Previous runs</h3></div></div>{history.map((item) => <div className="history-row" key={item.id}><span>{new Date(item.created_at).toLocaleString()}</span><span>{item.files_discovered} files</span><span>{item.optimized_duration_ms} ms warm</span><span>{item.path}</span></div>)}</div>}</section>; }
function DuplicatesView({ activePath }: { activePath: string }) {
  const [groups, setGroups] = useState<{ size: number; fingerprint: string; files: string[] }[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const scan = async () => {
    if (!activePath) return;
    setLoading(true); setError("");
    try { const result = await api<{ groups: typeof groups }>("/duplicates", { method: "POST", body: JSON.stringify({ path: activePath }) }); setGroups(result.groups); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to scan for duplicates."); }
    finally { setLoading(false); }
  };
  return <section className="content"><div className="page-heading compact"><div><p className="eyebrow">DUPLICATES</p><h2>Exact duplicate groups</h2><p className="muted">Only identical SHA-256 content fingerprints are grouped. LazyDesk never deletes duplicates automatically.</p></div><button className="button primary" onClick={scan} disabled={!activePath || loading}>{loading ? <LoaderCircle className="spin" size={15} /> : <Copy size={15} />}Scan duplicates</button></div>{error && <div className="alert inline">{error}</div>}{groups.length ? <div className="duplicate-list">{groups.map((group) => <div className="duplicate-card" key={group.fingerprint}><div className="duplicate-heading"><strong>{group.files.length} exact matches</strong><span>{formatBytes(group.size)} each · SHA-256 {group.fingerprint.slice(0, 16)}…</span></div>{group.files.map((file) => <div className="duplicate-file" key={file}><File size={15} />{file}</div>)}</div>)}</div> : <EmptyState icon={<Copy size={26} />} title={activePath ? "No duplicate groups found" : "Select a workspace first"} text={activePath ? "Run an exact-content duplicate scan when you are ready." : "Scan a folder from Workspace before checking duplicates."} />}</section>;
}
function SettingsView({ ollama, models, selectedModel, onModelChange }: { ollama: boolean; models: string[]; selectedModel: string | null; onModelChange: (model: string | null) => void }) {
  const [saving, setSaving] = useState(false);
  const saveModel = async (model: string) => {
    setSaving(true);
    try { const result = await api<{ model: string | null }>("/settings", { method: "PATCH", body: JSON.stringify({ model: model || null }) }); onModelChange(result.model); }
    finally { setSaving(false); }
  };
  return <section className="content"><div className="page-heading compact"><div><p className="eyebrow">SETTINGS</p><h2>Local preferences</h2><p className="muted">Keep control of where LazyDesk can look and which local model it can use.</p></div></div><div className="settings-list"><div className="setting-row"><div><strong>Ollama connection</strong><span>Optional local inference for richer classification suggestions.</span></div><span className={`status-pill ${ollama ? "online" : ""}`}><span className="status-dot" />{ollama ? "Connected" : "Unavailable"}</span></div><div className="setting-row"><div><strong>Classification model</strong><span>Only installed local models appear here; LazyDesk never downloads one.</span></div><select value={selectedModel ?? ""} onChange={(event) => saveModel(event.target.value)} disabled={saving || !models.length} aria-label="Ollama classification model"><option value="">Deterministic fallback</option>{models.map((model) => <option key={model} value={model}>{model}</option>)}</select></div><div className="setting-row"><div><strong>Filesystem safety</strong><span>Backend validates every path and blocks destinations outside the active workspace.</span></div><ShieldCheck size={20} className="green-icon" /></div><div className="setting-row"><div><strong>Storage</strong><span>Activity, benchmark, plan, and cache history are stored in local SQLite.</span></div><HardDrive size={20} /></div></div></section>;
}
function EmptyState({ icon, title, text }: { icon: ReactNode; title: string; text: string }) { return <div className="empty-state">{icon}<strong>{title}</strong><span>{text}</span></div>; }
