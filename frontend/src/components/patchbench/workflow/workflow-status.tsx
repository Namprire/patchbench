import { Activity, Check, CircleSlash, Clock3, HelpCircle, TriangleAlert, XCircle, type LucideIcon } from "lucide-react";
import type { WorkflowNodeStatus } from "../../../types/dashboard";
import { cn } from "../../../lib/utils";

type WorkflowStatusVisual = { label: string; icon: LucideIcon; shell: string; rail: string; edge: string; tag: string; fill: string };
const statusMap: Record<WorkflowNodeStatus, WorkflowStatusVisual> = {
  initial: { label: "Initial", icon: Clock3, shell: "border-edge-subtle bg-surface", rail: "", edge: "stroke-[hsl(var(--border-subtle))]", tag: "text-ink-subtle", fill: "bg-surface" },
  queued: { label: "Queued", icon: Clock3, shell: "border-edge bg-surface", rail: "", edge: "stroke-[hsl(var(--border-default))]", tag: "text-ink-muted", fill: "bg-surface" },
  running: { label: "Running", icon: Activity, shell: "border-brand-border border-l-2 border-l-brand bg-brand-soft", rail: "border-l-2 border-l-brand", edge: "stroke-[hsl(var(--accent))]", tag: "text-brand-text", fill: "bg-brand-soft" },
  success: { label: "Complete", icon: Check, shell: "border-success-border bg-success-soft", rail: "", edge: "stroke-[hsl(var(--success))]", tag: "text-success-text", fill: "bg-success-soft" },
  warning: { label: "Review", icon: TriangleAlert, shell: "border-dashed border-warn bg-warn-soft", rail: "", edge: "stroke-[hsl(var(--warn))]", tag: "text-warn", fill: "bg-warn-soft" },
  error: { label: "Error", icon: XCircle, shell: "border-danger border-l-2 border-l-danger bg-danger-soft", rail: "border-l-2 border-l-danger", edge: "stroke-[hsl(var(--danger))]", tag: "text-danger", fill: "bg-danger-soft" },
  skipped: { label: "Skipped", icon: CircleSlash, shell: "border-dotted border-edge bg-surface text-ink-subtle", rail: "", edge: "stroke-[hsl(var(--border-subtle))]", tag: "text-ink-subtle", fill: "bg-surface" },
  unavailable: { label: "Unknown", icon: HelpCircle, shell: "border-dotted border-edge-subtle bg-surface text-ink-subtle", rail: "", edge: "stroke-[hsl(var(--border-subtle))]", tag: "text-ink-subtle", fill: "bg-surface" },
};

export function workflowStatus(status: WorkflowNodeStatus) { return statusMap[status]; }
export function WorkflowStatusLabel({ status, className }: { status: WorkflowNodeStatus; className?: string }) { const value=statusMap[status]; const Icon=value.icon; return <span className={cn("inline-flex items-center gap-1.5 t-label-xs",value.tag,className)}><Icon className="h-3.5 w-3.5" aria-hidden="true"/>{value.label}</span>; }
