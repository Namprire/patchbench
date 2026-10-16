import { useMemo, useState } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import type { RunDetail, RunSummary, WorkflowNodeData } from "../../../types/dashboard";
import { WorkflowCanvas } from "./workflow-canvas";
import { WorkflowDiagnostics } from "./workflow-diagnostics";
import { WorkflowErrorBoundary } from "./workflow-error-boundary";
import { WorkflowFallback } from "./workflow-fallback";
import { WorkflowHealthSummary } from "./workflow-health-summary";
import { WorkflowInspector } from "./workflow-inspector";
import { architectureFlowElements, runFlowElements } from "./workflow-layout";
import { WorkflowLegend } from "./workflow-legend";
import { StageStrip } from "./stage-strip";
import { ExecutionConsole } from "../console/execution-console";

export function WorkflowExplorer({mode,run,selectedStageId,onSelectStage,dockConsole=false}:{mode:"architecture"|"trace";run?:RunSummary|null;selectedStageId?:string|null;onSelectStage?:(id:string|null)=>void;dockConsole?:boolean}){
  const architecture=useMemo(()=>architectureFlowElements(),[]);
  const data=useMemo(()=>mode==="trace"&&run?{...runFlowElements(run.workflow),workflow:run.workflow}:architecture,[architecture,mode,run]);
  const [internalSelected,setInternalSelected]=useState<WorkflowNodeData|null>(null);
  const controlled=onSelectStage!==undefined;
  const selected=controlled?(data.workflow.nodes.find((node)=>node.id===selectedStageId)??null):internalSelected;
  const setSelected=(node:WorkflowNodeData|null)=>{if(controlled)onSelectStage?.(node?.id??null);else setInternalSelected(node)};
  const context=mode==="trace"&&run?`${run.case_id}/${run.run_id}`:"architecture";
  const selectById=(id:string)=>setSelected(data.workflow.nodes.find((node)=>node.id===id)??null);
  const detailRun=run&&"artifacts" in run?run as RunDetail:null;
  return <ReactFlowProvider><div className="space-y-4"><WorkflowHealthSummary workflow={data.workflow} run={mode==="trace"?run:null}/>{mode==="trace"&&<StageStrip workflow={data.workflow} selectedStageId={selected?.id??null} onSelect={selectById}/>}<div className="flex flex-col gap-4"><div className="order-1 md:order-0"><WorkflowErrorBoundary context={context}><div className="overflow-hidden border border-edge-strong xl:grid xl:grid-cols-[minmax(0,1fr)_var(--inspector-w)]"><WorkflowCanvas key={context} workflow={data.workflow} initialNodes={data.nodes} initialEdges={data.edges} mode={mode} caseId={run?.case_id} runId={run?.run_id} selectedId={selected?.id??null} onSelect={setSelected}/><WorkflowInspector node={selected} diagnostics={data.workflow.diagnostics} run={run}/></div></WorkflowErrorBoundary></div><div className="order-2 md:order-1"><WorkflowInspector mobileTrigger node={selected} diagnostics={data.workflow.diagnostics} run={run}/></div><div className="order-3 md:order-2"><WorkflowLegend/></div><div className="order-4 md:order-3"><WorkflowDiagnostics diagnostics={data.workflow.diagnostics} onSelect={selectById}/></div><div className="order-0 md:order-4"><WorkflowFallback workflow={data.workflow} selectedId={selected?.id??null} onSelect={selectById}/></div></div>{dockConsole&&detailRun&&<ExecutionConsole run={detailRun} mode="docked" defaultCollapsed selectedStageId={selected?.id??null} onSelectStage={(id)=>setSelected(id?data.workflow.nodes.find((node)=>node.id===id)??null:null)}/>}</div></ReactFlowProvider>;
}
