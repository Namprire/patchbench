import { useEffect, useMemo, useState, type ReactNode } from "react";
import { AlertTriangle, Check, Copy, Terminal } from "lucide-react";
import { toast } from "sonner";
import { useDashboard } from "../data/dashboard";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Checkbox } from "../components/ui/checkbox";
import { Input } from "../components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select";
import { EmptyState } from "../components/patchbench/empty-state";
import { PageError, PageHeading, PageLoading } from "./page-state";
import { buildCommand, isValidRunId } from "../lib/workflow-config";

export { buildCommand, isValidRunId } from "../lib/workflow-config";

export function CommandBuilderPage(){
  const {dashboard,loading,error}=useDashboard();
  const[caseId,setCaseId]=useState("demo-label");const[agent,setAgent]=useState("claude-code");const[runId,setRunId]=useState("demo-model-r1");const[model,setModel]=useState("qwen3-coder-next");const[promptMode,setPromptMode]=useState("issue_with_hints");const[maxTurns,setMaxTurns]=useState(40);const[timeout,setTimeoutValue]=useState(900);const[container,setContainer]=useState(true);const[live,setLive]=useState(true);const[debug,setDebug]=useState(true);
  useEffect(()=>{if(dashboard?.cases.length&&!dashboard.cases.some((item)=>item.case_id===caseId))setCaseId(dashboard.cases[0].case_id)},[dashboard,caseId]);
  const duplicate=dashboard?.runs.some((run)=>run.case_id===caseId&&run.run_id===runId)??false;const valid=isValidRunId(runId);
  const command=useMemo(()=>buildCommand({caseId,agent,runId,model,promptMode,maxTurns,timeout,container,live,debug}),[caseId,agent,runId,model,promptMode,maxTurns,timeout,container,live,debug]);
  if(loading)return <PageLoading/>;if(error)return <PageError message={error}/>;if(!dashboard)return <EmptyState/>;
  const toggles=[["Container",container,setContainer],["Live",live,setLive],["Debug",debug,setDebug]] as const;
  return <>
    <PageHeading eyebrow="Coding agent evaluation" title="Run command" description="Configure a benchmark run and copy the generated CLI command."/>
    <div className="grid gap-6 lg:grid-cols-[.8fr_1.2fr]">
      <div className="space-y-4 border border-edge bg-surface p-5">
        <Field label="Case"><Select value={caseId} onValueChange={setCaseId}><SelectTrigger><SelectValue/></SelectTrigger><SelectContent>{dashboard.cases.map((item)=><SelectItem key={item.case_id} value={item.case_id}>{item.case_id}</SelectItem>)}</SelectContent></Select></Field>
        <Field label="Agent"><Select value={agent} onValueChange={setAgent}><SelectTrigger><SelectValue/></SelectTrigger><SelectContent>{["dummy","gold","claude-code","container-command"].map((item)=><SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectContent></Select></Field>
        <Field label="Run ID"><Input aria-label="Run ID" value={runId} onChange={(event)=>setRunId(event.target.value)}/><p className="mt-1 t-mono-xs text-ink-subtle">One non-empty path component; existing IDs are immutable.</p></Field>
        {agent==="claude-code"&&<Field label="Requested model"><Input value={model} onChange={(event)=>setModel(event.target.value)}/></Field>}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Prompt mode"><Select value={promptMode} onValueChange={setPromptMode}><SelectTrigger><SelectValue/></SelectTrigger><SelectContent><SelectItem value="issue_only">issue_only</SelectItem><SelectItem value="issue_with_hints">issue_with_hints</SelectItem></SelectContent></Select></Field>
          {agent==="claude-code"&&<Field label="Max turns"><Input className="font-mono text-right tabular-nums" type="number" min={1} value={maxTurns} onChange={(event)=>setMaxTurns(Number(event.target.value))}/></Field>}
        </div>
        <Field label="Wall-clock timeout"><Input className="font-mono text-right tabular-nums" type="number" min={1} value={timeout} onChange={(event)=>setTimeoutValue(Number(event.target.value))}/></Field>
        <div className="flex flex-wrap gap-5">{toggles.map(([label,value,setter])=><label key={label} className="flex min-h-11 items-center gap-2 t-label md:min-h-8"><Checkbox checked={value} onCheckedChange={(checked)=>setter(Boolean(checked))}/>{label}</label>)}</div>
      </div>
      <div>
        <section className="border border-edge"><header className="flex items-center justify-between gap-3 border-b border-edge bg-surface px-4 py-3"><div className="flex items-center gap-2"><Terminal className="h-4 w-4"/><span className="t-label">Command preview</span></div><Button disabled={!valid||duplicate} variant="outline" size="sm" onClick={()=>{void navigator.clipboard.writeText(command);toast.success("Command copied")}}><Copy className="h-3.5 w-3.5"/>Copy command</Button></header><div className="bg-invert p-5"><pre data-testid="command-preview" className="whitespace-pre-wrap break-words t-console text-invert-foreground">{command}</pre></div><div className="grid grid-cols-3 border-t border-edge bg-surface"><Summary label="Turns" value={agent==="claude-code"?maxTurns:"n/a"}/><Summary label="Wall clock" value={`${timeout}s`}/><Summary label="Prompt" value={promptMode.replace("issue_","")}/></div></section>
        <p className="mt-4 border-l-2 border-l-edge-strong pl-3 t-label-xs text-ink-subtle">Execution happens outside the browser.</p>
        {duplicate&&<Alert variant="danger" className="mt-4"><AlertTriangle className="mb-2 h-4 w-4 text-danger"/><AlertTitle>Run ID already exists</AlertTitle><AlertDescription>PatchBench protects immutable evidence. Choose a new run ID.</AlertDescription></Alert>}
        {!valid&&<Alert variant="danger" className="mt-4"><AlertTriangle className="mb-2 h-4 w-4 text-danger"/><AlertTitle>Invalid run ID</AlertTitle><AlertDescription>Use a single non-empty path component without slash or backslash.</AlertDescription></Alert>}
        {valid&&!duplicate&&<p className="mt-4 flex items-center gap-2 t-body-sm text-ink-muted"><Check className="h-3.5 w-3.5"/>Command is ready to copy. Execution remains outside the browser.</p>}
      </div>
    </div>
  </>;
}

function Field({label,children}:{label:string;children:ReactNode}){return <label className="block"><span className="mb-1 block t-label text-ink-subtle">{label}</span>{children}</label>}
function Summary({label,value}:{label:string;value:ReactNode}){return <div className="border-l border-edge-subtle px-4 py-3 first:border-l-0"><span className="t-label-xs text-ink-subtle">{label}</span><p className="mt-1 t-mono-sm tabular-nums">{value}</p></div>}
