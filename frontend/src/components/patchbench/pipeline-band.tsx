import { ArrowDown, ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";

export type PipelineStep = {
  label: string;
  href: string;
  tip: string;
  count: number;
};

export function PipelineBand({ steps }: { steps: PipelineStep[] }) {
  return (
    <div className="border border-edge bg-surface">
      <div className="grid grid-cols-1 sm:grid-cols-5">
        {steps.map((step, index) => (
          <Tooltip key={step.label}>
            <TooltipTrigger asChild>
              <Link
                to={step.href}
                className="group relative min-h-[100px] border-b border-edge-subtle p-3 outline-none transition-colors duration-2 last:border-b-0 hover:bg-hover focus-visible:z-10 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:border-b-0 sm:border-r sm:last:border-r-0"
              >
                <span className="t-label-xs text-ink-subtle">{String(index + 1).padStart(2, "0")}</span>
                <span className="mt-2 flex min-h-7 max-w-[14ch] items-start justify-between gap-2 t-label text-ink">{step.label}</span>
                <span className="mt-2 block t-mono-sm text-ink-muted">{step.count}</span>
                {index < steps.length - 1 && (
                  <span aria-hidden="true" className="absolute -bottom-2 left-1/2 z-10 flex h-4 w-5 -translate-x-1/2 items-center justify-center bg-surface text-ink-subtle transition-colors duration-2 group-hover:text-brand sm:-right-2 sm:bottom-auto sm:left-auto sm:top-1/2 sm:h-5 sm:w-4 sm:translate-x-0 sm:-translate-y-1/2">
                    <ArrowDown className="h-3.5 w-3.5 sm:hidden"/><ArrowRight className="hidden h-3.5 w-3.5 sm:block" />
                  </span>
                )}
              </Link>
            </TooltipTrigger>
            <TooltipContent>{step.tip}</TooltipContent>
          </Tooltip>
        ))}
      </div>
    </div>
  );
}
