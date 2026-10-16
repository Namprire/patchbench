import { useEffect, useRef } from "react";
import type { Workflow } from "../../../types/dashboard";
import { cn } from "../../../lib/utils";
import { workflowStatus } from "./workflow-status";

const settled = new Set(["success", "warning", "error", "skipped"]);
const shortLabels:Record<string,string>={
  "Case Source / Provenance":"Source","Frozen Case":"Case","Solver Worktree":"Worktree","Prompt Context":"Prompt",
  "Agent Harness":"Agent","Agent Tools":"Tools","Patch Capture":"Patch","Deterministic Evaluation":"Evaluate","Result Artifacts":"Result",
  "Case selection":"Case","Workspace setup":"Workspace","Prompt assembly":"Prompt","Agent launch":"Launch",
  "Agent execution":"Agent","Tool activity":"Tools","Patch capture":"Capture","Termination":"End",
  "Deterministic evaluation":"Evaluate","Result artifacts":"Results",
};

export function StageStrip({ workflow, selectedStageId, onSelect }: { workflow: Workflow; selectedStageId: string | null; onSelect: (id: string) => void }) {
  const frontierRef=useRef<HTMLButtonElement|null>(null);
  const frontierIndex = workflow.nodes.reduce((latest, node, index) => ["initial", "queued", "unavailable"].includes(node.status) ? latest : index, -1);
  const frontier = frontierIndex >= 0 ? workflow.nodes[frontierIndex] : null;
  useEffect(()=>{const button=frontierRef.current;const scroller=button?.closest<HTMLElement>("[data-stage-strip]");if(button&&scroller&&scroller.scrollWidth>scroller.clientWidth)scroller.scrollLeft=Math.max(0,button.offsetLeft-(scroller.clientWidth-button.offsetWidth)/2)},[frontier?.id]);
  if (!workflow.nodes.length) return <div className="flex h-12 items-center border border-edge bg-surface px-3 t-label text-ink-subtle md:h-10">No stage data in this export</div>;
  return <div className="flex h-12 min-w-0 items-stretch border border-edge bg-surface md:h-10">
    <div data-stage-strip className="stage-scroll-mask min-w-0 flex-1 overflow-x-auto">
    <div className="flex h-full min-w-max items-center px-3 pr-5">
      {workflow.nodes.map((node,index)=>{
        const visual=workflowStatus(node.status);const Icon=visual.icon;const beyond=frontierIndex>=0&&index>frontierIndex;const selected=selectedStageId===node.id;
        const short=shortLabels[node.label]??node.label.replace(/\s+(stage|artifacts?)$/i,"");
        return <div key={node.id} className="flex items-center"><button ref={index===frontierIndex?frontierRef:undefined} aria-label={`${node.label}: ${visual.label}`} title={node.label} onClick={()=>onSelect(node.id)} className={cn("flex h-11 items-center gap-1.5 rounded-sm border px-2 outline-none hover:border-edge-strong hover:bg-hover focus-visible:ring-2 focus-visible:ring-ring md:h-6",visual.shell,index===frontierIndex&&"border-l-2 border-l-brand",beyond&&"border-dotted border-edge-subtle bg-surface text-ink-subtle",selected&&"border-edge-strong bg-chosen")}><Icon className="h-2.5 w-2.5"/><span className="t-label-xs text-ink-subtle">{String(index+1).padStart(2,"0")}</span><span className={cn("whitespace-nowrap t-label-xs",index===frontierIndex&&"text-brand-text")}>{short}</span>{node.status==="running"&&<span className="h-1.5 w-1.5 rounded-full bg-brand motion-safe:animate-pulse"/>}</button>{index<workflow.nodes.length-1&&<span aria-hidden="true" className={cn("flex w-3 items-center text-[9px]",settled.has(node.status)?"text-ink-muted":"text-ink-subtle")}><span className={cn("h-px w-2",settled.has(node.status)?"bg-edge-strong":"bg-edge-subtle")}/><span>›</span></span>}</div>;
      })}
    </div>
    </div>
    {frontier&&<span className="hidden shrink-0 items-center border-l border-edge-subtle px-3 t-label text-ink-subtle md:flex">Last recorded stage: <span className="ml-1 text-brand-text">{shortLabels[frontier.label]??frontier.label}</span></span>}
  </div>;
}
