import { Clock3 } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";

export type DataFreshnessProps = {
  timestamp: string | null;
  schemaVersion: number | null;
  repositoryScope: string;
  caseCount: number | null;
  runCount: number | null;
  loading?: boolean;
};

export function exportTime(timestamp: string | null) {
  if (!timestamp) return "TIMESTAMP UNAVAILABLE";
  return `${new Date(timestamp).toLocaleString(undefined, { timeZone: "UTC", year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" })} UTC`;
}

function shortExportTime(timestamp: string | null) {
  if (!timestamp) return "TIMESTAMP UNAVAILABLE";
  return `${new Date(timestamp).toLocaleDateString(undefined, { timeZone: "UTC", year: "numeric", month: "short", day: "2-digit" })} UTC`;
}

export function DataFreshness({ timestamp, schemaVersion, repositoryScope, caseCount, runCount, loading = false }: DataFreshnessProps) {
  const time = loading ? "——" : exportTime(timestamp);
  const facts=<><span>SCHEMA v{loading ? "—" : schemaVersion ?? "?"}</span><span aria-hidden="true">·</span><span>{loading ? "—" : caseCount ?? 0} CASES</span><span aria-hidden="true">·</span><span>{loading ? "—" : runCount ?? 0} RUNS</span><span aria-hidden="true">·</span><span>{loading ? "——" : repositoryScope}</span></>;
  const timestampNode=<Tooltip><TooltipTrigger asChild><span className="inline-flex cursor-help items-center gap-1.5"><Clock3 className="h-3 w-3" /><span className="xl:hidden">EXPORT {loading?"——":shortExportTime(timestamp)}</span><span className="hidden xl:inline">EXPORT {time}</span></span></TooltipTrigger><TooltipContent>{timestamp ? `Exported ${new Date(timestamp).toLocaleString()}` : "Export timestamp unavailable"}</TooltipContent></Tooltip>;
  return <div className="scroll-mask h-7 overflow-x-auto [scrollbar-width:none]">
    <div className="flex h-7 min-w-max items-center gap-2 px-4 t-mono-xs text-ink-subtle md:hidden"><span className="text-brand-text">READ-ONLY</span><span aria-hidden="true">·</span>{facts}</div>
    <div className="hidden h-7 min-w-max items-center gap-2 px-6 t-mono-xs text-ink-subtle md:flex xl:px-8"><span className="hidden lg:inline-flex">{timestampNode}</span><span className="hidden lg:inline" aria-hidden="true">·</span>{facts}<span aria-hidden="true">·</span><span className="text-brand-text">READ-ONLY</span></div>
  </div>;
}
