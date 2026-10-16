import { memo } from "react";
import { Activity, Bot, Box, ClipboardList, FileCode2, FileInput, Flag, GitBranch, Hammer, ScrollText, Wrench } from "lucide-react";
import { Position, type NodeProps } from "@xyflow/react";
import { compactLabel, cn } from "../../../lib/utils";
import type { WorkflowFlowNode } from "./workflow-layout";
import { BaseNode, LabeledHandle, NodeTooltip } from "./react-flow-ui";
import { workflowStatus } from "./workflow-status";

const icons = { source:GitBranch,case:Box,worktree:Hammer,prompt:FileInput,agent:Bot,tools:Wrench,patch:FileCode2,termination:Flag,evaluation:ClipboardList,result:ScrollText,posthoc:Activity } as const;

function metricText(metrics: Record<string, unknown>) {
  return Object.entries(metrics).flatMap(([key, value]) => {
    if (value == null) return [];
    if (Array.isArray(value)) return value.length ? [`${value.length} ${compactLabel(key)}`] : [];
    if (typeof value === "object") {
      const count = Object.keys(value).length;
      return count ? [`${count} ${compactLabel(key)}`] : [];
    }
    return [`${compactLabel(key)} ${String(value)}`];
  }).slice(0, 2).join(" · ");
}

export const WorkflowNode = memo(({ data, selected }: NodeProps<WorkflowFlowNode>) => {
  const status = workflowStatus(data.status);
  const KindIcon = icons[data.kind as keyof typeof icons] ?? Activity;
  const StatusIcon = status.icon;
  const metrics = metricText(data.metrics);
  const hasFooter = data.diagnostic_codes.length > 0 || data.frontier;
  return (
    <NodeTooltip content={data.status_reason ?? data.summary}>
      <BaseNode
        data-workflow-node-id={data.id}
        title={`${data.label} · ${status.label} · ${data.id}`}
        tabIndex={0}
        role="button"
        aria-label={`${data.label}: ${status.label}`}
        className={cn(
          "group w-[240px] cursor-pointer overflow-hidden text-ink transition-colors duration-2 ease-std hover:border-edge-strong hover:bg-hover focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
          status.shell,
          data.beyondFrontier && "border-dotted border-edge-subtle bg-surface text-ink-subtle",
          selected && "bg-chosen ring-2 ring-edge-strong ring-offset-1 ring-offset-canvas",
        )}
      >
        <LabeledHandle type="target" position={Position.Left} label={`${data.label} input`} />
        <div className="flex h-6 items-center gap-2 border-b border-edge-subtle px-2">
          <span className="t-label-xs text-ink-subtle">{String(data.ordinal).padStart(2, "0")}</span>
          <KindIcon className="h-3 w-3 text-ink-muted" aria-hidden="true" />
          <span className="truncate t-label-xs text-ink-muted">{data.kind}</span>
          <span className={cn("ml-auto flex items-center gap-1", status.tag)} title={status.label}>
            <StatusIcon className="h-3 w-3" aria-hidden="true" />
            {data.status === "running" && <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-brand motion-safe:animate-pulse" />}
          </span>
        </div>
        <div className={cn("px-2 py-2", metrics ? "min-h-[51px]" : "min-h-[35px]")}>
          <h3 className="truncate t-body-sm font-semibold">{data.label}</h3>
          {metrics && <p className="mt-1 truncate t-mono-xs text-ink-subtle">{metrics}</p>}
        </div>
        {hasFooter && <div className="flex h-5 items-center border-t border-edge-subtle px-2">
          <span className={cn("t-label-xs text-ink-subtle", data.diagnosticSeverity === "warning" && "text-warn", data.diagnosticSeverity === "error" && "text-danger")}>{data.diagnostic_codes.length} diag</span>
          {data.frontier && <span aria-label="Last recorded stage" className="ml-auto h-3 border-l-2 border-l-brand" />}
        </div>}
        <LabeledHandle type="source" position={Position.Right} label={`${data.label} output`} />
      </BaseNode>
    </NodeTooltip>
  );
});
WorkflowNode.displayName = "WorkflowNode";
