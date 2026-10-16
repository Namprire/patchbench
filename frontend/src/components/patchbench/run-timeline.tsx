import { Activity, Check, FileCode2, Flag, Play, Square } from "lucide-react";
import type { RunDetail } from "../../types/dashboard";
import { compactLabel, formatDuration } from "../../lib/utils";

export function RunTimeline({ run }: { run: RunDetail }) {
  const events = [
    { label: "Session start", detail: "Harness process launched", at: "0.0s", icon: Play, present: true },
    { label: "First activity", detail: run.activity_observed ? `${run.event_count ?? 0} structured events observed` : "No structured activity observed", at: formatDuration(run.first_event_seconds), icon: Activity, present: run.activity_observed === true },
    { label: "Patch captured", detail: run.partial_patch_captured ? "Partial patch preserved" : "Source diff persisted", at: formatDuration(run.last_event_seconds), icon: FileCode2, present: run.patch_captured },
    { label: "Termination", detail: compactLabel(run.termination_reason), at: formatDuration(run.runtime_seconds), icon: Square, present: true },
    { label: "Evaluation", detail: run.evaluation_skipped ? `Skipped · ${compactLabel(run.evaluation_skip_reason)}` : run.resolved ? "Deterministic tests passed" : "Deterministic evaluation recorded", at: "", icon: run.resolved ? Check : Flag, present: run.evaluation_skipped !== null },
  ];
  return <ol className="relative ml-2 border-l border-edge-subtle">{events.filter((item) => item.present).map(({label,detail,at,icon:Icon}) => <li key={label} className="relative ml-6 pb-7 last:pb-0"><span className="absolute -left-[31px] top-0 flex h-3 w-3 items-center justify-center border border-edge-strong bg-surface text-ink"><Icon className="h-2 w-2" /></span><div className="flex items-baseline justify-between gap-4"><p className="t-body-sm font-medium">{label}</p><span className="t-mono-xs text-ink-subtle">{at}</span></div><p className="mt-1 t-body-sm text-ink-muted">{detail}</p></li>)}</ol>;
}
