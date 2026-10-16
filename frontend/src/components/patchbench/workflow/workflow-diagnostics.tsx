import { CircleAlert, Info, TriangleAlert } from "lucide-react";
import type { WorkflowDiagnostic } from "../../../types/dashboard";
import { cn } from "../../../lib/utils";
import { Alert, AlertDescription, AlertTitle } from "../../ui/alert";

export function WorkflowDiagnostics({ diagnostics, onSelect }: { diagnostics: WorkflowDiagnostic[]; onSelect?: (id: string) => void }) {
  const highest = diagnostics.some((item) => item.severity === "error") ? "error" : diagnostics.some((item) => item.severity === "warning") ? "warning" : "info";
  return <div>
    <h2 className="mb-3 t-label text-ink-subtle">Diagnostics <span className={cn(highest === "error" && "text-danger", highest === "warning" && "text-warn")}>({diagnostics.length})</span></h2>
    {!diagnostics.length ? <div className="border border-edge bg-surface p-3 t-body-sm text-ink-muted">No diagnostics for this run.</div> : <div className="space-y-2">{diagnostics.map((item,index)=>{
      const Icon=item.severity==="error"?CircleAlert:item.severity==="warning"?TriangleAlert:Info;
      return <Alert id={`diag-${item.code}`} tabIndex={-1} key={`${item.code}-${index}`} variant={item.severity==="error"?"danger":item.severity==="warning"?"warn":"strong"}>
        <div className="flex items-start gap-2"><Icon className={cn("mt-0.5 h-3.5 w-3.5 shrink-0",item.severity==="error"?"text-danger":item.severity==="warning"?"text-warn":"text-ink-muted")}/><div className="min-w-0"><AlertTitle className="t-mono-sm font-medium">{item.code}</AlertTitle><AlertDescription><p>{item.message}</p><p className="mt-2 t-mono-xs text-ink-subtle">Inspect {item.inspection_target} · {item.evidence.field}={String(item.evidence.value)}</p>{onSelect&&<button className="mt-2 t-label-xs text-brand-text underline-offset-4 hover:underline" onClick={()=>onSelect(item.node_id)}>→ Select stage</button>}</AlertDescription></div></div>
      </Alert>;
    })}</div>}
  </div>;
}
