import { useEffect, useState, type ReactNode } from "react";
import { ArrowLeft, ChevronDown, FileCode2, Hammer } from "lucide-react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useDashboard } from "../data/dashboard";
import type { RunDetail } from "../types/dashboard";
import { cn, compactLabel, formatCost, formatDuration, formatNumber } from "../lib/utils";
import { ArtifactTabs } from "../components/patchbench/artifact-tabs";
import { ExecutionConsole } from "../components/patchbench/console/execution-console";
import { EvaluationBadge, RunStatusBadge, TerminationBadge } from "../components/patchbench/status-badges";
import { StageStrip } from "../components/patchbench/workflow/stage-strip";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Badge } from "../components/ui/badge";
import { Progress } from "../components/ui/progress";
import { PageError, PageLoading } from "./page-state";
import { configFromRun, WORKFLOW_CONFIG_STORAGE_KEY } from "../lib/workflow-config";

const identityKey="patchbench.run.identity";
const Field=({label,value}:{label:string;value:string})=><div className="min-w-0 p-3"><dt className="t-label-xs text-ink-subtle">{label}</dt><dd className="mt-1 break-all t-mono-sm text-ink">{value}</dd></div>;
const SummaryField=({label,children}:{label:string;children:ReactNode})=><span className="flex min-w-0 items-baseline justify-between gap-3 whitespace-nowrap md:block"><span className="t-label-xs text-ink-subtle">{label}</span> <span className="truncate t-mono-sm text-ink">{children}</span></span>;
const StateCell=({label,children,detail,rail}:{label:string;children:ReactNode;detail:string;rail?:"warn"|"danger"})=><div className={cn("min-w-0 p-4",rail==="warn"&&"border-l-2 border-l-warn",rail==="danger"&&"border-l-2 border-l-danger")}><p className="mb-3 t-label text-ink-subtle">{label}</p>{children}<p className="mt-3 break-all t-mono-sm text-ink-muted">{detail}</p></div>;

export function RunDetailPage() {
  const {caseId="",runId=""}=useParams();const {loadRun}=useDashboard();const[params,setParams]=useSearchParams();
  const navigate=useNavigate();
  const [run,setRun]=useState<RunDetail|null>(null);const [error,setError]=useState<string|null>(null);const[selectedStageId,setSelectedStageId]=useState<string|null>(null);
  const [allFields,setAllFields]=useState(()=>localStorage.getItem(identityKey)==="open");
  useEffect(()=>{const controller=new AbortController();setRun(null);setError(null);setSelectedStageId(null);loadRun(caseId,runId,controller.signal).then(setRun).catch((reason:unknown)=>{if(!controller.signal.aborted)setError(reason instanceof Error?reason.message:"Unable to load run")});return()=>controller.abort()},[caseId,runId,loadRun]);
  if(error)return <PageError message={error}/>;if(!run)return <PageLoading/>;
  const turnProgress=run.configured_max_turns!=null&&run.reported_turns!=null?Math.min(100,run.reported_turns/run.configured_max_turns*100):0;
  const answerRun=run.case_type==="landb_answer";const raw=run.raw_sanitized_metadata;const answerCorrect=raw.answer_correct===true;const formatCorrect=raw.answer_format_correct===true;const readOnly=raw.read_only===true;
  const providerFailure=run.workflow.diagnostics.find((item)=>item.code==="AGENT_API_ERROR");
  const selectStage=(id:string|null)=>{setSelectedStageId(id);if(id){const next=new URLSearchParams(params);next.set("tab","workflow");setParams(next,{replace:true})}};
  const toggleFields=()=>setAllFields((current)=>{const next=!current;try{localStorage.setItem(identityKey,next?"open":"closed")}catch{/* Personal disclosure state is optional. */}return next});
  const buildFromRun=()=>{try{localStorage.setItem(WORKFLOW_CONFIG_STORAGE_KEY,JSON.stringify(configFromRun(run)))}catch{/* Continue with navigation. */}navigate("/workflow")};
  return <>
    <div className="mb-4 mt-4 flex items-center justify-between gap-3"><Link to="/runs" className="inline-flex min-h-11 items-center gap-2 t-label text-ink-muted hover:text-ink md:min-h-0"><ArrowLeft className="h-3.5 w-3.5"/>Back to runs</Link><Button variant="outline" size="sm" onClick={buildFromRun}><Hammer className="h-3.5 w-3.5"/>Use config in Build</Button></div>
    <h1 className="sr-only">Run details</h1>
    <section className="mb-4 border border-edge bg-surface">
      <header className="p-4"><div className="flex flex-wrap items-start justify-between gap-3"><p className="break-all t-mono-md"><span className="text-ink-muted">{run.case_id}</span><span className="mx-2 text-ink-subtle">/</span><span className="font-medium text-ink">{run.run_id}</span></p><Button variant="ghost" size="sm" onClick={toggleFields}>All fields<ChevronDown className={cn("h-3.5 w-3.5 transition-transform",allFields&&"rotate-180")}/></Button></div>
        <div className="mt-3 grid gap-1 text-ink-subtle sm:grid-cols-2 md:flex md:flex-wrap md:gap-x-2 md:gap-y-1"><SummaryField label="Agent">{run.agent??"unknown"}</SummaryField><span className="hidden md:inline">·</span><SummaryField label="Harness">{run.agent_harness??"unknown"}{run.agent_harness_version?` ${run.agent_harness_version}`:""}</SummaryField><span className="hidden md:inline">·</span><SummaryField label="Model">{run.actual_model??"unknown"}</SummaryField><span className="hidden md:inline">·</span><SummaryField label="Provider">{run.provider??"unknown"}{run.provider_host?` ${run.provider_host}`:""}</SummaryField><span className="hidden md:inline">·</span><SummaryField label="Prompt">{compactLabel(run.prompt_mode)}</SummaryField><span className="hidden md:inline">·</span><SummaryField label="Turns">{formatNumber(run.reported_turns)} / {formatNumber(run.configured_max_turns)}</SummaryField><span className="hidden md:inline">·</span><SummaryField label="Runtime">{formatDuration(run.runtime_seconds)}</SummaryField></div>
        {run.configured_max_turns!=null&&<div className="mt-3"><Progress value={turnProgress} warn={run.termination_reason==="max_turns"} className="h-0.5"/></div>}
      </header>
      {allFields&&<dl className="grid border-t border-edge-subtle divide-y divide-edge-subtle sm:grid-cols-2 sm:divide-x sm:divide-y-0 lg:grid-cols-4"><Field label="Agent" value={run.agent??"unknown"}/><Field label="Harness" value={run.agent_harness??"unknown"}/><Field label="Harness version" value={run.agent_harness_version??"unknown"}/><Field label="Requested model" value={run.requested_model??"default-selected"}/><Field label="Actual model" value={run.actual_model??"unknown"}/><Field label="Provider" value={`${run.provider??"unknown"}${run.provider_host?` · ${run.provider_host}`:""}`}/><Field label="Prompt mode" value={compactLabel(run.prompt_mode)}/><Field label="Turn budget" value={formatNumber(run.configured_max_turns)}/><Field label="Reported turns" value={formatNumber(run.reported_turns)}/><Field label="Runtime" value={formatDuration(run.runtime_seconds)}/><Field label="Input tokens" value={formatNumber(run.input_tokens)}/><Field label="Output tokens" value={formatNumber(run.output_tokens)}/><Field label="Reported cost" value={formatCost(run.reported_cost_usd)}/></dl>}
    </section>
    {run.historical&&<Alert variant="warn" className="mb-4"><AlertDescription>This run uses an earlier result schema. Missing fields are shown as unknown.</AlertDescription></Alert>}
    {providerFailure&&<Alert variant="danger" className="mb-4"><AlertDescription>{providerFailure.message}</AlertDescription></Alert>}
    <div className="mb-4"><StageStrip workflow={run.workflow} selectedStageId={selectedStageId} onSelect={(id)=>selectStage(id)}/></div>
    <section aria-label="Run state" className="mb-7 grid border border-edge bg-surface divide-y divide-edge-subtle sm:grid-cols-2 sm:divide-x sm:divide-y-0 lg:grid-cols-4">
      <StateCell label="Termination" detail={compactLabel(run.execution_detail)} rail={run.termination_reason==="max_turns"||run.termination_reason==="timeout"?"warn":undefined}><TerminationBadge reason={run.termination_reason}/></StateCell>
      {answerRun?<><StateCell label="Retrieval" detail={`${run.mcp_tool_call_count} MCP call(s)`}><Badge variant="quiet">{compactLabel(String(raw.condition??run.mcp_condition))}</Badge></StateCell><StateCell label="Answer evaluation" detail={`JSON ${formatCorrect?"valid":"invalid"} · read-only ${readOnly?"yes":"no"}`} rail={answerCorrect?undefined:"danger"}><Badge variant={answerCorrect?"solid":"danger"}>{answerCorrect?"answer correct":"answer incorrect"}</Badge></StateCell></>:<><StateCell label="Patch" detail={`Applied: ${run.patch_applied==null?"unknown":run.patch_applied?"yes":"no"}`} rail={run.partial_patch_captured?"warn":undefined}><Badge variant={run.partial_patch_captured?"warn":run.patch_captured?"solid":"absent"}><FileCode2 className="h-3 w-3"/>{run.partial_patch_captured?"Partial patch":run.patch_captured?"Patch captured":"No patch"}</Badge></StateCell><StateCell label="Evaluation" detail={compactLabel(run.evaluation_skip_reason)} rail={run.evaluation_skipped?"warn":undefined}><EvaluationBadge skipped={run.evaluation_skipped} applied={run.patch_applied} resolved={run.resolved}/></StateCell></>}
      <StateCell label="Outcome" detail={answerRun?`format ${formatCorrect?"valid":"invalid"}`:compactLabel(run.failure_mode)} rail={run.resolved===false?"danger":undefined}><RunStatusBadge resolved={run.resolved}/></StateCell>
    </section>
    <ArtifactTabs run={run} selectedStageId={selectedStageId} onSelectStage={selectStage}/>
    <ExecutionConsole run={run} mode="docked" selectedStageId={selectedStageId} onSelectStage={selectStage}/>
  </>;
}
