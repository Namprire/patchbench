import type { LucideIcon } from "lucide-react";
import { ArrowUpRight } from "lucide-react";
import { Link } from "react-router-dom";
import type { ReactNode } from "react";
import { cn } from "../../lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";

type MetricCardProps = { label: string; value: ReactNode; accessibleValue?: string; note?: string; icon: LucideIcon; href?: string; primary?: boolean; compactValue?: boolean; tooltip?: string; standalone?: boolean };

export function MetricCard({ label, value, accessibleValue, note, icon: Icon, href, primary = false, compactValue = false, tooltip, standalone = true }: MetricCardProps) {
  const content = <>
    <div className="relative min-w-0">
      <div className="min-w-0">
        <p className="flex h-8 items-start overflow-hidden pr-7 t-label text-ink-subtle">{label}</p>
        <p className={cn("metric-card-value mt-1 max-w-full overflow-hidden text-ellipsis whitespace-nowrap tabular-nums text-ink", compactValue ? "font-mono text-xl font-medium leading-7" : primary ? "t-metric-lg" : "t-metric")}>{value}</p>
        {note && <p className="mt-1 t-body-sm text-ink-muted">{note}</p>}
      </div>
      <div className="absolute right-0 top-0 h-4 w-4 shrink-0 text-ink-subtle">
        <Icon className="absolute inset-0 h-4 w-4 transition-opacity duration-1 group-hover:opacity-0 group-focus-visible:opacity-0" />
        {href && <ArrowUpRight aria-hidden="true" className="absolute inset-0 h-3.5 w-3.5 text-brand opacity-0 transition-opacity duration-1 group-hover:opacity-100 group-focus-visible:opacity-100" />}
      </div>
    </div>
  </>;
  const shell = cn("metric-card min-h-[112px] min-w-0 overflow-hidden bg-surface p-4 transition-[background-color,box-shadow] duration-2", standalone && "border border-edge");
  if (!href) return <div className={shell}>{content}</div>;
  const readableValue=accessibleValue??(typeof value==="string"||typeof value==="number"?String(value):"");
  const link = <Link to={href} aria-label={`${label}${readableValue?`: ${readableValue}`:""}. Open related runs.`} className={cn("group block outline-none hover:bg-hover hover:shadow-[inset_2px_0_0_hsl(var(--accent))] active:bg-pressed focus-visible:bg-hover focus-visible:shadow-[inset_2px_0_0_hsl(var(--accent))]", shell)}>{content}</Link>;
  return tooltip ? <Tooltip><TooltipTrigger asChild>{link}</TooltipTrigger><TooltipContent>{tooltip}</TooltipContent></Tooltip> : link;
}
