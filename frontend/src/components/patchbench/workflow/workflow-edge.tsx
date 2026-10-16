import { memo } from "react";
import { BaseEdge, EdgeLabelRenderer, getSmoothStepPath, type Edge, type EdgeProps } from "@xyflow/react";
import type { WorkflowEdgeStatus, WorkflowNodeStatus } from "../../../types/dashboard";

type Data = { status?: WorkflowEdgeStatus; sourceStatus?: WorkflowNodeStatus; mode?: "architecture" | "trace" } & Record<string, unknown>;
type WorkflowEdgeType = Edge<Data, "workflowEdge">;

const visuals: Record<WorkflowEdgeStatus, { color: string; width: number; dash?: string }> = {
  inactive: { color: "hsl(var(--border-subtle))", width: 1 },
  active: { color: "hsl(var(--accent))", width: 2 },
  completed: { color: "hsl(var(--border-strong))", width: 1.5 },
  warning: { color: "hsl(var(--warn))", width: 1.5, dash: "6 4" },
  error: { color: "hsl(var(--danger))", width: 1.5 },
  skipped: { color: "hsl(var(--border-subtle))", width: 1, dash: "2 4" },
};

export const WorkflowEdge = memo(({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, label, animated }: EdgeProps<WorkflowEdgeType>) => {
  const [path, labelX, labelY] = getSmoothStepPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, borderRadius: 4 });
  const status = data?.status ?? "inactive";
  const visual = visuals[status];
  const shouldAnimate = Boolean(animated && data?.mode === "trace" && data.sourceStatus === "running");
  const markerId = `workflow-${status}-arrow`;
  return <>
    <defs>
      <marker id={markerId} markerWidth="12" markerHeight="12" refX="10" refY="6" orient="auto" markerUnits="strokeWidth">
        <path d="M 0 0 L 12 6 L 0 12 z" fill={visual.color} />
      </marker>
    </defs>
    {status === "error" && <path d={path} fill="none" stroke="hsl(var(--danger) / .18)" strokeWidth={5} />}
    <BaseEdge id={id} path={path} markerEnd={`url(#${markerId})`} style={{ stroke: visual.color, strokeWidth: visual.width, strokeDasharray: shouldAnimate ? "4 4" : visual.dash }} />
    {label && <EdgeLabelRenderer><span className="pointer-events-none absolute bg-canvas px-1 t-label-xs text-ink-subtle" style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}>{String(label)}</span></EdgeLabelRenderer>}
  </>;
});
WorkflowEdge.displayName = "WorkflowEdge";
