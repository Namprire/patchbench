import { ChevronDown, ChevronRight } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import type { RunSummary, WorkflowDiagnostic, WorkflowNodeData } from "../../../types/dashboard";
import { cn, compactLabel, formatDuration } from "../../../lib/utils";
import { Button, buttonVariants } from "../../ui/button";
import { ScrollArea } from "../../ui/scroll-area";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from "../../ui/sheet";
import { WorkflowStatusLabel } from "./workflow-status";

const artifactTab:Record<string,string>={prompt:"prompt",agent_patch:"patch",trajectory:"trajectory",test_log:"test",agent_events:"events",agent_debug:"debug",human_reference_patch:"human"};
type SectionKey = "summary" | "timing" | "metrics" | "diagnostics" | "artifacts" | "raw";
type SectionState = Record<SectionKey, boolean>;
const defaults: SectionState = { summary:true,timing:true,metrics:true,diagnostics:true,artifacts:true,raw:false };
const storageKey = "patchbench.inspector.sections";

function initialSections(): SectionState {
  try { return { ...defaults, ...JSON.parse(localStorage.getItem(storageKey) ?? "{}") as Partial<SectionState> }; }
  catch { return defaults; }
}

const Row=({label,children}:{label:string;children:ReactNode})=><div className="grid grid-cols-[110px_1fr] gap-3 border-b border-edge-subtle py-2"><span className="t-label-xs text-ink-subtle">{label}</span><span className="break-all text-right t-mono-sm text-ink">{children}</span></div>;

function Section({id,label,open,onToggle,children}:{id:SectionKey;label:string;open:boolean;onToggle:(id:SectionKey)=>void;children:ReactNode}) {
  const Icon=open?ChevronDown:ChevronRight;
  return <section><button type="button" aria-expanded={open} onClick={()=>onToggle(id)} className="flex h-11 w-full items-center justify-between border-b border-edge-subtle px-4 t-label text-ink-subtle outline-none hover:bg-hover focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring md:h-7"><span>{label}</span><Icon className="h-3 w-3" /></button>{open&&<div className="px-4 py-3">{children}</div>}</section>;
}

function InspectorBody({node,diagnostics,run}:{node:WorkflowNodeData|null;diagnostics:WorkflowDiagnostic[];run?:RunSummary|null}){
  const [sections,setSections]=useState<SectionState>(initialSections);
  useEffect(()=>{if(node?.status==="error")setSections((current)=>({...current,diagnostics:true}))},[node?.id,node?.status]);
  const toggle=(id:SectionKey)=>setSections((current)=>{const next={...current,[id]:!current[id]};try{localStorage.setItem(storageKey,JSON.stringify(next))}catch{/* Personal inspector state is optional. */}return next});
  if(!node)return <div><div className="flex h-24 items-center justify-center border-b border-edge-subtle bg-canvas"><span className="t-label-xs text-ink-subtle">No stage selected</span></div><p className="p-5 t-body-sm text-ink-muted">Select a stage to view its evidence.</p></div>;
  const relevant=diagnostics.filter((item)=>item.node_id===node.id);
  const rail=node.status==="running"?"border-l-2 border-l-brand":node.status==="error"?"border-l-2 border-l-danger":node.status==="warning"?"border-l-2 border-l-warn":"";
  return <ScrollArea className="h-full"><div><header className={cn("border-b border-edge-subtle p-5",rail)}><p className="t-label text-ink-subtle">{node.kind}</p><h2 className="mt-2 t-subtitle">{node.label}</h2><div className="mt-3"><WorkflowStatusLabel status={node.status}/></div><p className="mt-2 break-all t-mono-xs text-ink-subtle">{node.id}</p>{node.status_reason&&<p className="mt-3 t-body-sm text-ink-muted">{node.status_reason}</p>}</header>
    <Section id="summary" label="Summary" open={sections.summary} onToggle={toggle}><p className="t-body-sm text-ink-muted">{node.summary}</p></Section>
    <Section id="timing" label="Timing" open={sections.timing} onToggle={toggle}><Row label="Started">{node.started_at??"unknown"}</Row><Row label="Completed">{node.completed_at??"unknown"}</Row><Row label="Duration">{formatDuration(node.duration_seconds)}</Row></Section>
    <Section id="metrics" label="Metrics" open={sections.metrics} onToggle={toggle}>{Object.entries(node.metrics).length?Object.entries(node.metrics).map(([key,value])=><Row key={key} label={compactLabel(key)}>{value==null?"unknown":typeof value==="object"?JSON.stringify(value):String(value)}</Row>):<p className="t-body-sm text-ink-muted">No metrics for this stage.</p>}</Section>
    <Section id="diagnostics" label="Diagnostics" open={sections.diagnostics} onToggle={toggle}>{relevant.length?relevant.map((item)=><div key={item.code} className={cn("mb-2 border border-edge p-3 last:mb-0",item.severity==="error"?"border-l-2 border-l-danger":item.severity==="warning"?"border-l-2 border-l-warn":"border-l-2 border-l-edge-strong")}><p className="t-mono-xs text-ink">{item.code}</p><p className="mt-1 t-body-sm text-ink-muted">{item.message}</p></div>):<p className="t-body-sm text-ink-muted">No diagnostics for this stage.</p>}</Section>
    <Section id="artifacts" label="Artifacts" open={sections.artifacts} onToggle={toggle}><div className="flex flex-wrap gap-2">{node.artifact_refs.length&&run?node.artifact_refs.map((artifact)=><Link key={artifact} className={buttonVariants({variant:"outline",size:"sm"})} to={`/runs/${encodeURIComponent(run.case_id)}/${encodeURIComponent(run.run_id)}?tab=${artifactTab[artifact]??"raw"}`}>{compactLabel(artifact)}</Link>):<p className="t-body-sm text-ink-muted">This stage has no exported artifact.</p>}</div></Section>
    <Section id="raw" label="Raw" open={sections.raw} onToggle={toggle}><pre className="max-h-72 overflow-auto whitespace-pre-wrap border border-edge-subtle bg-inset p-2 t-console text-ink">{JSON.stringify(node,null,2)}</pre></Section>
  </div></ScrollArea>;
}

export function WorkflowInspector({node,diagnostics,run,mobileTrigger=false}:{node:WorkflowNodeData|null;diagnostics:WorkflowDiagnostic[];run?:RunSummary|null;mobileTrigger?:boolean}){
  if(mobileTrigger)return <Sheet><SheetTrigger asChild><Button variant="outline" className="h-11 w-full xl:hidden">Inspect selected stage</Button></SheetTrigger><SheetContent className="inset-x-0 bottom-0 top-auto max-h-[85vh] w-full max-w-none border-l-0 border-t border-edge-strong p-0 md:inset-x-auto md:bottom-auto md:right-0 md:top-0 md:h-full md:max-h-none md:w-[90vw] md:max-w-[480px] md:border-l md:border-t-0"><SheetHeader className="sr-only"><SheetTitle>Workflow stage inspector</SheetTitle><SheetDescription>Normalized evidence for the selected workflow stage.</SheetDescription></SheetHeader><InspectorBody node={node} diagnostics={diagnostics} run={run}/></SheetContent></Sheet>;
  return <aside aria-label="Selected workflow node inspector" className="hidden h-full min-h-0 border-l border-edge-strong bg-surface xl:block"><InspectorBody node={node} diagnostics={diagnostics} run={run}/></aside>;
}
