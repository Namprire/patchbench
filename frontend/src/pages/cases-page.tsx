import { useEffect, useState, type ReactNode } from "react";
import { Check, CircleDashed, FileCode2, FlaskConical, GitCommit, LoaderCircle, Timer, X } from "lucide-react";
import { useDashboard } from "../data/dashboard";
import type { CaseDetail, CaseSummary } from "../types/dashboard";
import { compactLabel } from "../lib/utils";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Checkbox } from "../components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { Input } from "../components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { CodeViewer, DiffViewer } from "../components/patchbench/code-viewer";
import { EmptyState } from "../components/patchbench/empty-state";
import { PageError, PageHeading, PageLoading } from "./page-state";

function CaseCard({value,onOpen}:{value:CaseSummary;onOpen:()=>void}) {
  const validation=value.validation_status===true
    ?{label:"validated",variant:"default" as const,icon:<Check className="h-3 w-3"/>}
    :value.validation_status===false
      ?{label:"failed",variant:"danger" as const,icon:<X className="h-3 w-3"/>}
      :{label:"unknown",variant:"dashed" as const,icon:<CircleDashed className="h-3 w-3"/>};
  return <Card className="group flex flex-col border-edge bg-surface transition-colors hover:bg-hover">
    <CardHeader><div className="flex items-start justify-between gap-4"><div className="min-w-0"><p className="truncate t-mono-xs text-ink-subtle">{value.repository??"repository unknown"}</p><CardTitle className="mt-2 break-all t-mono-md">{value.case_id}</CardTitle></div><div className="flex flex-col items-end gap-1"><Badge variant="quiet">{value.case_type==="landb_answer"?"answer":"coding"}</Badge><Badge variant={validation.variant}>{validation.icon}{validation.label}</Badge></div></div></CardHeader>
    <CardContent className="flex flex-1 flex-col"><div className="border-t border-edge-subtle pt-4"><p className="min-h-10 line-clamp-2 t-body-sm text-ink-muted">{value.issue_title}</p></div><div className="mt-5 grid grid-cols-2 border-y border-edge-subtle"><div className="py-3"><span className="t-label-xs text-ink-subtle">Runs</span><p className="mt-1 t-mono-sm tabular-nums">{value.run_count}</p></div><div className="border-l border-edge-subtle py-3 pl-4"><span className="t-label-xs text-ink-subtle">Gold files</span><p className="mt-1 t-mono-sm tabular-nums">{value.gold_files_changed.length}</p></div></div><Button variant="outline" className="mt-4 w-full group-hover:border-edge-strong" onClick={onOpen}>Open case</Button></CardContent>
  </Card>;
}

function Fact({icon,label,value}:{icon:ReactNode;label:string;value:string|number}) {
  const display=value==="—s"?"—":value;
  return <div className="min-w-0 border-b border-edge-subtle px-4 py-3 last:border-b-0 sm:border-b-0 sm:border-l sm:first:border-l-0"><div className="flex items-center gap-2 t-label-xs text-ink-subtle">{icon}{label}</div><p className="mt-2 break-all t-mono-xs">{display}</p></div>;
}

export function CasesPage() {
  const {dashboard,loading,error,loadCase,refresh}=useDashboard();
  const [selected,setSelected]=useState<string|null>(null);
  const [detail,setDetail]=useState<CaseDetail|null>(null);
  const [validationContainer,setValidationContainer]=useState(false);
  const [landbMcpRepo,setLandbMcpRepo]=useState("../landb-mcp");
  const [validating,setValidating]=useState(false);
  const [validationResult,setValidationResult]=useState<{validated:boolean;error?:string|null}|null>(null);
  const [validationError,setValidationError]=useState<string|null>(null);

  useEffect(()=>{
    setValidationContainer(false);setValidationResult(null);setValidationError(null);
    if(!selected){setDetail(null);return;}
    const controller=new AbortController();
    loadCase(selected,controller.signal).then(setDetail).catch(()=>setDetail(null));
    return()=>controller.abort();
  },[selected,loadCase]);

  const validateSelected=async()=>{
    if(!detail||validating)return;
    setValidating(true);setValidationResult(null);setValidationError(null);
    try{
      const response=await fetch(`/api/cases/${encodeURIComponent(detail.case_id)}/validate`,{
        method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({case_id:detail.case_id,container:detail.case_type==="coding"&&validationContainer,landb_mcp_repo:landbMcpRepo}),
      });
      let payload:{result?:{validated?:boolean;error?:string|null};error?:string;indexing_error?:string};
      try{payload=await response.json() as typeof payload}catch{throw new Error(`Validation service returned an invalid response (${response.status})`)}
      if(!response.ok||typeof payload.result?.validated!=="boolean")throw new Error(payload.error??`Case validation failed (${response.status})`);
      setValidationResult({validated:payload.result.validated,error:payload.result.error});
      if(payload.indexing_error)throw new Error(payload.indexing_error);
      await refresh();
      setDetail(await loadCase(detail.case_id));
    }catch(reason){setValidationError(reason instanceof Error?reason.message:"Case validation failed")}
    finally{setValidating(false)}
  };

  if(loading)return <PageLoading/>;
  if(error)return <PageError message={error}/>;
  if(!dashboard)return <EmptyState/>;
  return <>
    <PageHeading eyebrow="Agent evaluation" title="Benchmark cases" description="Frozen coding and answer tasks used for reproducible agent evaluation."/>
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{dashboard.cases.map((item)=><CaseCard key={item.case_id} value={item} onOpen={()=>setSelected(item.case_id)}/>)}</div>
    <Dialog open={Boolean(selected)} onOpenChange={(open)=>!open&&setSelected(null)}><DialogContent className="inset-0 h-full max-h-none w-full max-w-none translate-x-0 translate-y-0 overflow-y-auto p-4 md:left-1/2 md:top-1/2 md:h-auto md:max-h-[90vh] md:w-[calc(100%-2rem)] md:max-w-[720px] md:-translate-x-1/2 md:-translate-y-1/2 md:p-6 lg:max-w-[1100px]">{detail?<>
      <DialogHeader className="pr-11"><DialogTitle className="t-title">Case details</DialogTitle><p className="t-mono-sm text-ink-subtle">{detail.case_id}</p><DialogDescription className="t-body text-ink-muted">{detail.issue_title}</DialogDescription></DialogHeader>
      <div className="grid border border-edge sm:grid-cols-2 lg:grid-cols-4"><Fact icon={<GitCommit className="h-3.5 w-3.5"/>} label="Base commit" value={detail.base_commit??"not applicable"}/><Fact icon={<GitCommit className="h-3.5 w-3.5"/>} label="Fix commit" value={detail.fix_commit??"not applicable"}/><Fact icon={<Timer className="h-3.5 w-3.5"/>} label="Timeout" value={`${detail.evaluation.timeout_seconds??"—"}s`}/><Fact icon={<FlaskConical className="h-3.5 w-3.5"/>} label="Runs" value={detail.run_count}/></div>
      <section aria-label="Case validation" className="mt-4 border border-edge bg-surface p-4"><div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="t-label text-ink">Validate case</h3><p className="mt-1 t-body-sm text-ink-muted">Run the same frozen-case checks as <span className="t-mono-xs">patchbench validate-case</span>. Requires the local workflow service.</p></div><Button onClick={()=>void validateSelected()} disabled={validating||detail.case_type==="landb_answer"&&!landbMcpRepo.trim()}>{validating?<LoaderCircle className="h-3.5 w-3.5 animate-spin"/>:<Check className="h-3.5 w-3.5"/>}{validating?"Validating…":"Validate case"}</Button></div>
        {detail.case_type==="landb_answer"?<label className="mt-4 block max-w-xl"><span className="t-label-xs text-ink-subtle">LanDB MCP repository</span><Input className="mt-1" value={landbMcpRepo} onChange={(event)=>setLandbMcpRepo(event.target.value)} aria-label="LanDB MCP repository"/></label>:<label className="mt-4 flex items-center gap-2 t-body-sm text-ink-muted"><Checkbox checked={validationContainer} onCheckedChange={(value)=>setValidationContainer(value===true)} aria-label="Validate in Podman"/>Validate in Podman</label>}
        {validationResult&&<Alert className="mt-3" variant={validationResult.validated?"strong":"danger"}><AlertDescription>{validationResult.validated?"Case validation passed and the dashboard was refreshed.":validationResult.error??"Case validation failed."}</AlertDescription></Alert>}
        {validationError&&<Alert className="mt-3" variant="danger"><AlertDescription>{validationError}</AlertDescription></Alert>}
      </section>
      <Tabs defaultValue="issue" className="mt-4"><TabsList><TabsTrigger value="issue">Issue</TabsTrigger><TabsTrigger value="metadata">Safe provenance</TabsTrigger><TabsTrigger value="evaluation">Evaluation summary</TabsTrigger>{detail.human_reference_patch&&<TabsTrigger value="human">Reference patch</TabsTrigger>}</TabsList><TabsContent value="issue"><CodeViewer content={detail.issue_text} label="Solver-safe issue"/></TabsContent><TabsContent value="metadata"><CodeViewer content={JSON.stringify(detail.safe_provenance,null,2)} label="Safe provenance" language="json"/></TabsContent><TabsContent value="evaluation"><div className="border border-edge bg-surface p-5"><p className="t-label text-ink-subtle">Test command</p><p className="mt-3 t-mono-sm">{detail.evaluation.test_command??"not applicable"}</p><div className="mt-5 grid border-y border-edge-subtle sm:grid-cols-3"><p className="py-3 t-body-sm"><span className="t-label-xs text-ink-subtle">Workdir</span><br/><span className="t-mono-xs">{detail.evaluation.workdir??"repository root"}</span></p><p className="border-edge-subtle py-3 t-body-sm sm:border-l sm:pl-4"><span className="t-label-xs text-ink-subtle">Setup commands</span><br/><span className="t-mono-xs tabular-nums">{detail.evaluation.setup_command_count}</span></p><p className="border-edge-subtle py-3 t-body-sm sm:border-l sm:pl-4"><span className="t-label-xs text-ink-subtle">Gold files</span><br/><span className="t-mono-xs">{detail.gold_files_changed.map(compactLabel).join(", ")||"none"}</span></p></div></div></TabsContent>{detail.human_reference_patch&&<TabsContent value="human"><div className="mb-3 flex items-center gap-2 border border-l-2 border-edge border-l-warn bg-surface px-4 py-3 t-body-sm text-ink-muted"><FileCode2 className="h-4 w-4"/>Researcher-only reference. Never supplied to agents.</div><DiffViewer content={detail.human_reference_patch} label="Reference patch"/></TabsContent>}</Tabs>
    </>:<PageLoading/>}</DialogContent></Dialog>
  </>;
}
