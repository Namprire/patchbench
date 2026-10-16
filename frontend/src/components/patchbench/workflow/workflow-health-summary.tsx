import { Activity, Check, HelpCircle, TriangleAlert, XCircle } from "lucide-react";
import type { RunSummary, Workflow } from "../../../types/dashboard";
import { cn, compactLabel, formatDuration, formatNumber } from "../../../lib/utils";
import { applicableProgress } from "./workflow-layout";

const healthVisual = {
  running: { icon: Activity, className: "text-brand-text" },
  healthy: { icon: Check, className: "text-ink" },
  degraded: { icon: TriangleAlert, className: "text-warn" },
  stalled: { icon: TriangleAlert, className: "text-warn" },
  error: { icon: XCircle, className: "text-danger" },
  unknown: { icon: HelpCircle, className: "text-ink-subtle" },
};

function formatAge(value:number|null){if(value==null)return"unknown";if(value<86400)return `${formatDuration(value)} ago`;const days=Math.floor(value/86400);const hours=Math.floor(value%86400/3600);return `${days}d ${hours}h ago`}

export function WorkflowHealthSummary({ workflow, run }: { workflow: Workflow; run?: RunSummary | null }) {
  const progress = applicableProgress(workflow);
  const activityAge = formatAge(workflow.last_activity_age_seconds);
  const visual = healthVisual[workflow.health];
  const HealthIcon = visual.icon;
  const cells = [
    { title: "Health", value: compactLabel(workflow.health), content: <span className={cn("flex items-center gap-1", visual.className)}><HealthIcon className="h-3 w-3" />{compactLabel(workflow.health)}{workflow.health === "running" && <span className="h-1.5 w-1.5 rounded-full bg-brand motion-safe:animate-pulse" />}</span> },
    { title: "Outcome", value: compactLabel(workflow.task_outcome) },
    { title: "Stages", value: `${progress.completed} of ${progress.total} stages` },
    ...(run ? [{ title: "Termination", value: compactLabel(run.termination_reason) }] : []),
  ];
  return <div aria-label="Workflow status summary" className={cn("border border-edge bg-surface", workflow.health === "error" && "border-l-2 border-l-danger", workflow.health === "unknown" && "border-dotted border-edge-subtle")}>
    <div className="flex flex-col md:h-9 md:flex-row md:items-stretch">
      <div className="min-w-0 flex-1 truncate border-b border-edge-subtle px-3 py-2 t-mono-sm uppercase text-ink-muted md:self-center md:border-b-0 md:py-0">{run ? `${run.case_id} · ${run.run_id}` : "PatchBench pipeline"}</div>
      <div className="grid grid-cols-2 md:flex md:shrink-0 md:items-stretch">{cells.map((cell) => <div key={cell.title} className="flex min-h-11 flex-col justify-center border-t border-edge-subtle px-3 odd:border-r md:min-h-9 md:border-l md:border-t-0 md:odd:border-r-0"><span className="t-label-xs text-ink-subtle">{cell.title}</span><span className={cn("t-label",cell.title==="Health"&&visual.className)}>{"content" in cell ? cell.content : cell.value}</span></div>)}</div>
    </div>
    <div className="overflow-x-auto border-t border-edge-subtle bg-canvas"><div className="flex h-7 min-w-max items-center px-3 t-mono-xs uppercase text-ink-subtle">{run ? `${formatNumber(run.reported_turns)} of ${formatNumber(run.configured_max_turns)} turns · ${formatDuration(run.runtime_seconds)} · ${formatNumber(run.event_count)} events · activity ${activityAge} · ${compactLabel(workflow.health_source)}` : "Pipeline reference"}</div></div>
  </div>;
}
