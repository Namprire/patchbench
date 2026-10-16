import { Activity, Ban, Check, CircleDashed, Clock, FileCheck2, FileWarning, Pause, X } from "lucide-react";
import { compactLabel } from "../../lib/utils";
import { Badge } from "../ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";

function Explained({children,explanation}:{children:React.ReactNode;explanation:string}) { return <Tooltip><TooltipTrigger asChild><span className="inline-flex cursor-help">{children}</span></TooltipTrigger><TooltipContent className="max-w-64">{explanation}</TooltipContent></Tooltip>; }
export function RunStatusBadge({ resolved }: { resolved: boolean | null }) {
  const badge=<Badge variant={resolved ? "solid" : resolved === false ? "danger" : "absent"}>{resolved ? <Check className="h-3 w-3" /> : resolved === false ? <X className="h-3 w-3" /> : <CircleDashed className="h-3 w-3" />}{resolved ? "resolved" : resolved === false ? "unresolved" : "unknown"}</Badge>;
  return resolved===null?<Explained explanation="Outcome metadata is unavailable in this exported result.">{badge}</Explained>:badge;
}
export function TerminationBadge({ reason }: { reason: string | null }) {
  const Icon = reason === "completed" ? Check : reason === "max_turns" ? Pause : reason?.includes("timeout") ? Clock : CircleDashed;
  const badge=<Badge variant={reason === "completed" ? "solid" : reason === null ? "absent" : "warn"}><Icon className="h-3 w-3" />{reason?compactLabel(reason):"unknown"}</Badge>;
  return reason===null?<Explained explanation="Termination metadata is unavailable for this historical or incomplete run.">{badge}</Explained>:badge;
}
export function EvaluationBadge({ skipped, applied, resolved }: { skipped: boolean | null; applied: boolean | null; resolved: boolean | null }) {
  if (skipped) return <Badge variant="absent"><Ban className="h-3 w-3" />skipped</Badge>;
  if (resolved) return <Badge variant="solid"><FileCheck2 className="h-3 w-3" />tests passed</Badge>;
  if (applied === false) return <Badge variant="warn"><FileWarning className="h-3 w-3" />not applied</Badge>;
  if(applied===null)return <Explained explanation="Evaluation or patch-application metadata is unavailable in this export."><Badge variant="absent"><Activity className="h-3 w-3" />unknown</Badge></Explained>;
  return <Badge variant="quiet"><Activity className="h-3 w-3" />evaluated</Badge>;
}
