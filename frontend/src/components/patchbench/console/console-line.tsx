import { ChevronDown, ChevronRight } from "lucide-react";
import { useState, type ReactNode } from "react";
import { cn } from "../../../lib/utils";
import type { ConsoleLine as ConsoleLineRecord } from "./console-lines";

const severityLabel = { stage:"STAGE",err:"ERR",warn:"WARN",ok:"OK",info:"INFO" } as const;
const severityClass = { stage:"text-brand-text",err:"text-danger",warn:"text-warn",ok:"text-ink",info:"text-ink-muted" } as const;

function displayTime(value: string | null) {
  if (!value) return "--:--:--";
  const clock=value.includes("T")?value.split("T")[1]?.replace(/Z|[+-]\d{2}:?\d{2}$/,""):value;
  return clock ?? value;
}

function highlight(message: string, search: string): ReactNode {
  const term=search.trim();if(!term)return message;const start=message.toLowerCase().indexOf(term.toLowerCase());if(start<0)return message;
  return <>{message.slice(0,start)}<mark className="bg-brand-soft text-brand-text">{message.slice(start,start+term.length)}</mark>{message.slice(start+term.length)}</>;
}

export function ConsoleLine({ line, focused, search, selectedStageId, onSelectStage }: { line: ConsoleLineRecord; focused: boolean; search: string; selectedStageId?: string | null; onSelectStage?: (id: string | null) => void }) {
  const [expanded,setExpanded]=useState(false);
  const expandable=line.source!=="workflow"&&typeof line.raw==="object"&&line.raw!==null;
  const runningBoundary=line.sev==="stage"&&line.message.startsWith("▶")&&typeof line.raw==="object"&&line.raw!==null&&"status" in line.raw&&line.raw.status==="running";
  const actionable=Boolean(expandable||line.diagnosticCode||line.sev==="stage"&&line.stageId);
  const selected=Boolean(line.stageId&&line.stageId===selectedStageId);
  const rail=selected?"border-l-2 border-l-brand":line.sev==="stage"?"border-l-2 border-l-brand":line.sev==="err"?"border-l-2 border-l-danger":line.sev==="warn"?"border-l-2 border-l-warn":"border-l-2 border-l-transparent";
  const activate=()=>{if(expandable)setExpanded((current)=>!current);if(line.stageId&&onSelectStage)onSelectStage(line.stageId)};
  const openDiagnostic=()=>{if(line.stageId&&onSelectStage)onSelectStage(line.stageId);const target=line.diagnosticCode?document.getElementById(`diag-${line.diagnosticCode}`):null;target?.scrollIntoView({block:"center"});target?.focus()};
  return <div data-console-severity={line.sev} className={cn("group",selected&&"bg-chosen")}>
    <div title={line.t??undefined} tabIndex={actionable?0:undefined} onClick={actionable?activate:undefined} onKeyDown={actionable?(event)=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();activate()}}:undefined} className={cn("grid min-w-0 items-start px-2 py-[3px] t-console hover:bg-hover focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring sm:min-w-[360px] sm:py-0 md:min-w-[500px]",focused?"grid-cols-[38px_minmax(250px,1fr)] sm:grid-cols-[70px_38px_minmax(250px,1fr)] md:grid-cols-[88px_44px_88px_minmax(0,1fr)] lg:grid-cols-[96px_44px_96px_64px_minmax(0,1fr)]":"grid-cols-[38px_minmax(250px,1fr)] sm:grid-cols-[70px_38px_minmax(250px,1fr)] md:grid-cols-[88px_44px_88px_minmax(0,1fr)] lg:grid-cols-[96px_44px_96px_minmax(0,1fr)]",rail,actionable&&"cursor-pointer")}>
      <span className="hidden whitespace-nowrap t-mono-xs text-ink-subtle group-hover:text-ink-muted sm:block">{displayTime(line.t)}</span>
      <span className={cn("flex items-center gap-1 t-label-xs",severityClass[line.sev])}>{runningBoundary&&<span className="h-1.5 w-1.5 rounded-full bg-brand motion-safe:animate-pulse"/>}{severityLabel[line.sev]}</span>
      <span className="hidden truncate t-mono-xs text-ink-muted md:block" title={line.stageId??"—"}>{line.stageId??"—"}</span>
      {focused&&<span className="hidden truncate t-label-xs text-ink-subtle lg:block">{line.source}</span>}
      <span className={cn("min-w-0 whitespace-pre-wrap break-words text-ink",line.sev==="stage"&&"font-medium")}>
        {expandable&&<span aria-hidden="true" className="mr-1 inline-flex w-3">{expanded?<ChevronDown className="h-3 w-3"/>:<ChevronRight className="h-3 w-3"/>}</span>}{highlight(line.message,search)}{line.diagnosticCode&&<button type="button" className="ml-1 inline-flex min-h-11 items-center t-label-xs text-brand-text underline-offset-4 hover:underline md:min-h-0" onClick={(event)=>{event.stopPropagation();openDiagnostic()}}>→ Diagnostics</button>}
      </span>
    </div>
    {expanded&&<pre className={cn("mb-1 ml-[38px] mr-2 max-h-60 overflow-auto border border-edge-subtle bg-canvas p-2 t-console text-ink sm:ml-[108px] md:ml-[220px]",focused&&"lg:ml-[302px]",!focused&&"lg:ml-[238px]")}>{JSON.stringify(line.raw,null,2)}</pre>}
  </div>;
}
