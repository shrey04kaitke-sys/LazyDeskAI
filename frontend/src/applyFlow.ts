// Pure helpers for the Apply confirmation, Undo and Settings screens (kept out of the components so they can be unit-tested).
export type PreviewMove = {
  action_id: string; source: string; destination: string; current_name: string; new_name: string; renamed: boolean;
  category: string; folder: string; creates_folder: boolean; size: number; name_source: string;
};
export type Preview = {
  plan_id: string; plan_revision: number; confirmation_token: string; expires_in_seconds: number;
  moves: PreviewMove[]; creates_folders: string[]; count: number; undo_note: string;
};
export type RunSummary = {
  id: string; plan_id: string; status: string; created_at: string; finished_at: string | null; summary: string;
  counts: Record<string, number>; total: number; can_undo: boolean; needs_attention: boolean;
};

export function moveLine(move: PreviewMove): string {
  const rename = move.renamed ? ` and renamed to ${move.new_name}` : "";
  return `${move.source} → ${move.destination}${rename}`;
}

export function expiryNote(seconds: number): string {
  const minutes = Math.max(1, Math.round(seconds / 60));
  return `This confirmation covers exactly these ${minutes === 1 ? "moves for about a minute" : `moves for about ${minutes} minutes`}; if anything changes, LazyDesk will refuse and ask you to review again.`;
}

export function statusLabel(status: string): string {
  return ({
    running: "Running", completed: "Applied", partial: "Partly applied", failed: "Not applied", needs_attention: "Needs attention",
    undoing: "Undoing", undone: "Undone", undo_partial: "Partly undone", undo_blocked: "Undo refused",
  } as Record<string, string>)[status] ?? status;
}

export function countsText(run: Pick<RunSummary, "counts">): string {
  const parts = Object.entries(run.counts).filter(([, n]) => n > 0).map(([state, n]) => `${n} ${state.replace("_", " ")}`);
  return parts.join(", ");
}

/** "Deterministic fallback" is a real saved choice, not just the empty option, so it needs its own way to be saved. */
export function fallbackControl(savedChoice: "model" | "none" | null | undefined, saving: boolean) {
  const saved = savedChoice === "none";
  return { label: saved ? "Deterministic fallback is saved" : "Always use deterministic fallback", disabled: saved || saving };
}
