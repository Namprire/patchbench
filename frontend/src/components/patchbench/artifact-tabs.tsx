import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import type { RunDetail } from "../../types/dashboard";
import { Tabs, TabsList, TabsTrigger } from "../ui/tabs";
import { CodeViewer, DiffViewer, LogViewer } from "./code-viewer";
import { CollapsibleJson } from "./console/collapsible-json";
import { ExecutionConsole } from "./console/execution-console";
import { PatchComparisonCard } from "./patch-comparison-card";
import { RunTimeline } from "./run-timeline";
import { TokenUsageCard } from "./token-usage-card";
import { WorkflowExplorer } from "./workflow/workflow-explorer";
import { WorkflowPreview } from "./workflow/workflow-preview";

type TabValue="overview"|"workflow"|"trajectory"|"events"|"mcp"|"prompt"|"answer"|"patch"|"human"|"comparison"|"test"|"debug"|"raw";
type Primary="overview"|"execution"|"artifacts"|"raw";
const tabPrimary:Record<TabValue,Primary>={overview:"overview",workflow:"execution",trajectory:"execution",events:"execution",mcp:"execution",prompt:"artifacts",answer:"artifacts",patch:"artifacts",human:"artifacts",comparison:"artifacts",test:"artifacts",debug:"artifacts",raw:"raw"};
const knownTabs=new Set(Object.keys(tabPrimary));

export function ArtifactTabs({ run, selectedStageId, onSelectStage }: { run: RunDetail; selectedStageId?: string|null; onSelectStage?: (id:string|null)=>void }) {
  const [params, setParams] = useSearchParams();
  const rawRequested=params.get("tab")??"overview";const requestedTab=(knownTabs.has(rawRequested)?rawRequested:"overview") as TabValue;
  const [tab,setTab]=useState<TabValue>(requestedTab);
  useEffect(()=>setTab(requestedTab),[requestedTab]);
  const primary=tabPrimary[tab];
  const choose=(value:TabValue)=>{setTab(value);const next=new URLSearchParams(params);if(value==="overview")next.delete("tab");else next.set("tab",value);setParams(next,{replace:true})};
  const choosePrimary=(value:string)=>choose(value==="overview"?"overview":value==="execution"?"workflow":value==="artifacts"?"prompt":"raw");
  const steps=(run.raw_sanitized_metadata as Record<string,unknown>).steps??run.artifacts.agent_events;
  let body;
  if(tab==="overview")body=<><div className="grid gap-4 lg:grid-cols-[1.2fr_.8fr]"><section className="border border-edge bg-surface"><header className="flex h-8 items-center border-b border-edge-subtle px-4 t-label text-ink-subtle">Activity</header><div className="p-5"><RunTimeline run={run}/></div></section><TokenUsageCard input={run.input_tokens} output={run.output_tokens} cache={run.cache_read_tokens} cost={run.reported_cost_usd}/></div><div className="mt-4"><WorkflowPreview run={run}/></div></>;
  else if(tab==="workflow")body=<WorkflowExplorer mode="trace" run={run} selectedStageId={selectedStageId} onSelectStage={onSelectStage}/>;
  else if(tab==="trajectory")body=<ExecutionConsole run={run} mode="focused" initialSource="trajectory" selectedStageId={selectedStageId} onSelectStage={onSelectStage}/>;
  else if(tab==="events")body=<ExecutionConsole run={run} mode="focused" initialSource="events" selectedStageId={selectedStageId} onSelectStage={onSelectStage}/>;
  else if(tab==="mcp")body=<ExecutionConsole run={run} mode="focused" initialSource="mcp" selectedStageId={selectedStageId} onSelectStage={onSelectStage}/>;
  else if(tab==="prompt")body=<CodeViewer content={run.artifacts.prompt} label="Solver-safe prompt"/>;
  else if(tab==="answer")body=<CodeViewer content={run.artifacts.answer} label="Final answer" language="json"/>;
  else if(tab==="patch")body=<DiffViewer content={run.artifacts.agent_patch}/>;
  else if(tab==="human")body=run.artifacts.human_reference_patch?<><div className="mb-3 border border-l-2 border-edge border-l-warn bg-surface px-4 py-3 t-body-sm text-ink-muted">Researcher-only reference. Never supplied to agents. Evaluation tests are not exported.</div><DiffViewer content={run.artifacts.human_reference_patch} label="Reference patch"/></>:null;
  else if(tab==="comparison")body=<PatchComparisonCard comparison={run.patch_comparison}/>;
  else if(tab==="test")body=<LogViewer content={run.artifacts.test_log} label="Test log"/>;
  else if(tab==="debug")body=<LogViewer content={run.artifacts.agent_debug} label="Debug log"/>;
  else body=<div className="space-y-2"><CollapsibleJson label="Sanitized metadata" value={run.raw_sanitized_metadata} showEmpty/><CollapsibleJson label="Workflow" value={run.workflow} showEmpty/><CollapsibleJson label="Steps" value={steps} showEmpty/></div>;
  return <div><Tabs value={primary} onValueChange={choosePrimary}><TabsList aria-label="Run detail sections"><TabsTrigger value="overview">Overview</TabsTrigger><TabsTrigger value="execution">Execution</TabsTrigger><TabsTrigger value="artifacts">Artifacts</TabsTrigger><TabsTrigger value="raw">Raw</TabsTrigger></TabsList></Tabs>{primary==="execution"&&<Tabs value={tab} onValueChange={(value)=>choose(value as TabValue)}><TabsList variant="secondary" aria-label="Execution evidence"><TabsTrigger variant="secondary" value="workflow">Execution trace</TabsTrigger><TabsTrigger variant="secondary" value="trajectory">Trajectory</TabsTrigger><TabsTrigger variant="secondary" value="events">Agent events</TabsTrigger><TabsTrigger variant="secondary" value="mcp">MCP activity</TabsTrigger></TabsList></Tabs>}{primary==="artifacts"&&<Tabs value={tab} onValueChange={(value)=>choose(value as TabValue)}><TabsList variant="secondary" aria-label="Run artifacts"><TabsTrigger variant="secondary" value="prompt">Prompt</TabsTrigger>{run.artifacts.answer&&<TabsTrigger variant="secondary" value="answer">Answer</TabsTrigger>}{run.case_type==="coding"&&<TabsTrigger variant="secondary" value="patch">Agent patch</TabsTrigger>}{run.artifacts.human_reference_patch&&<TabsTrigger variant="secondary" value="human">Reference patch</TabsTrigger>}{run.case_type==="coding"&&<TabsTrigger variant="secondary" value="comparison">Patch comparison</TabsTrigger>}{run.case_type==="coding"&&<TabsTrigger variant="secondary" value="test">Test log</TabsTrigger>}<TabsTrigger variant="secondary" value="debug">Debug log</TabsTrigger></TabsList></Tabs>}<div className="pt-5">{body}</div></div>;
}
