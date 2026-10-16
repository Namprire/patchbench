import type { Edge, Node, XYPosition } from "@xyflow/react";
import type { Workflow, WorkflowNodeData, WorkflowNodeStatus } from "../../../types/dashboard";

export const WORKFLOW_SCHEMA_VERSION = 1;

type FlowData = WorkflowNodeData & {
  ordinal: number;
  frontier: boolean;
  beyondFrontier: boolean;
  diagnosticSeverity?: "info" | "warning" | "error";
} & Record<string, unknown>;
export type WorkflowFlowNode = Node<FlowData, "workflowNode">;

const runPositions: Record<string, XYPosition> = {
  source: { x: 0, y: 30 }, case: { x: 280, y: 30 }, worktree: { x: 560, y: 30 },
  prompt: { x: 840, y: 30 }, agent: { x: 1120, y: 30 }, tools: { x: 0, y: 210 },
  patch: { x: 280, y: 210 }, termination: { x: 560, y: 210 }, evaluation: { x: 840, y: 210 },
  result: { x: 1120, y: 210 }, posthoc: { x: 1120, y: 390 },
};

export function runFlowElements(workflow: Workflow): { nodes: WorkflowFlowNode[]; edges: Edge[] } {
  const frontierIndex = workflow.nodes.reduce((latest, node, index) =>
    ["initial", "queued", "unavailable"].includes(node.status) ? latest : index, -1);
  const nodes: WorkflowFlowNode[] = workflow.nodes.map((node, index) => ({
    id: node.id, type: "workflowNode", position: runPositions[node.id] ?? { x: (index % 5) * 280, y: Math.floor(index / 5) * 180 + 30 },
    data: {
      ...node,
      ordinal: index + 1,
      frontier: index === frontierIndex,
      beyondFrontier: frontierIndex >= 0 && index > frontierIndex,
      diagnosticSeverity: workflow.diagnostics.some((item) => item.node_id === node.id && item.severity === "error")
        ? "error"
        : workflow.diagnostics.some((item) => item.node_id === node.id && item.severity === "warning")
          ? "warning"
          : workflow.diagnostics.some((item) => item.node_id === node.id)
            ? "info"
            : undefined,
    }, selectable: true, draggable: false,
  }));
  const statuses = new Map(workflow.nodes.map((node) => [node.id, node.status]));
  const edges: Edge[] = workflow.edges.map((edge) => ({
    id: edge.id, source: edge.source, target: edge.target, type: "workflowEdge",
    animated: edge.animated && statuses.get(edge.source) === "running",
    data: { status: edge.status, sourceStatus: statuses.get(edge.source), mode: "trace" }, label: edge.label ?? undefined,
  }));
  return { nodes, edges };
}

const architectureStages: Array<[string, string, string, string]> = [
  ["source", "source", "GitLab source", "Optional source ingestion before execution."],
  ["case", "case", "Frozen case", "Immutable issue, commits, source fix, and evaluation definition."],
  ["worktree", "worktree", "Solver worktree", "Isolated checkout at the pre-fix base commit."],
  ["prompt", "prompt", "Prompt context", "Only the approved issue and permitted hints reach the solver."],
  ["agent", "agent", "Agent harness", "Harness, model, provider, budgets, and lifecycle configuration."],
  ["tools", "tools", "Tool activity", "Structured, sanitized reads, searches, commands, and edits."],
  ["patch", "patch", "Patch capture", "The worktree diff, including partial patches."],
  ["termination", "termination", "Termination", "Completion, max turns, timeout, interruption, or CLI error."],
  ["evaluation", "evaluation", "Deterministic evaluation", "A fresh checkout applies the candidate and runs frozen tests."],
  ["result", "result", "Artifacts", "Structured result, logs, provenance, and comparison evidence."],
];
const architecturePositions: Record<string, XYPosition> = {
  source:{x:80,y:90},case:{x:380,y:90},worktree:{x:760,y:90},prompt:{x:1060,y:90},agent:{x:1360,y:90},
  tools:{x:760,y:310},patch:{x:1060,y:310},termination:{x:1360,y:310},evaluation:{x:1770,y:90},result:{x:2160,y:90},
};

export function architectureFlowElements(): { nodes: WorkflowFlowNode[]; edges: Edge[]; workflow: Workflow } {
  const normalizedNodes: WorkflowNodeData[] = architectureStages.map(([id,kind,label,summary]) => ({ id,kind,label,status:"success",status_reason:"Architecture stage",started_at:null,completed_at:null,duration_seconds:null,summary,metrics:{},artifact_refs:[],diagnostic_codes:[] }));
  const workflow: Workflow = { schema_version:1,health:"healthy",health_source:"authoritative",task_outcome:"diagnostic",started_at:null,last_activity_at:null,last_activity_age_seconds:null,completed_at:null,freshness_seconds:60,nodes:normalizedNodes,edges:normalizedNodes.slice(1).map((node,index)=>({id:`${normalizedNodes[index].id}-to-${node.id}`,source:normalizedNodes[index].id,target:node.id,status:"completed",label:null,animated:false})),diagnostics:[] };
  const nodes: WorkflowFlowNode[] = normalizedNodes.map((node,index)=>({id:node.id,type:"workflowNode",position:architecturePositions[node.id],data:{...node,ordinal:index+1,frontier:false,beyondFrontier:false},selectable:true,draggable:false}));
  const edges: Edge[] = workflow.edges.map((edge)=>({id:edge.id,source:edge.source,target:edge.target,type:"workflowEdge",data:{status:edge.status,sourceStatus:"success",mode:"architecture"}}));
  return { nodes,edges,workflow };
}

export function layoutStorageKey(mode: "architecture" | "trace" | "build", caseId?: string, runId?: string) { return `patchbench.workflow.v${WORKFLOW_SCHEMA_VERSION}.${mode}.${caseId ?? "all"}.${runId ?? "all"}`; }
export function applicableProgress(workflow: Workflow) { const applicable=workflow.nodes.filter((node)=>node.status!=="unavailable"); const completed=applicable.filter((node)=>["success","warning","error","skipped"].includes(node.status)); return {completed:completed.length,total:applicable.length}; }
export function statusForArchitecture(): WorkflowNodeStatus { return "success"; }
