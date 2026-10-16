import { forwardRef, useState, type HTMLAttributes, type ReactNode } from "react";
import {
  Handle,
  Panel,
  Position,
  useReactFlow,
  useStore,
  useViewport,
  type HandleProps,
  type Node,
} from "@xyflow/react";
import { Search } from "lucide-react";
import { cn } from "../../../lib/utils";
import { Input } from "../../ui/input";
import { Tooltip, TooltipContent, TooltipTrigger } from "../../ui/tooltip";

export const BaseNode = forwardRef<HTMLDivElement, HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn(
        "relative border border-edge bg-surface text-ink",
        className,
      )}
      {...props}
    />
  ),
);
BaseNode.displayName = "BaseNode";

export function LabeledHandle({ label, className, ...props }: HandleProps & { label: string }) {
  return <div className={cn("relative flex items-center gap-2 t-label-xs text-ink-subtle", className)}>
    <Handle className="!h-2 !w-2 !border-0 !bg-[hsl(var(--border-default))] group-hover:!bg-[hsl(var(--border-strong))]" {...props} />
    <span className="sr-only">{label}</span>
  </div>;
}

export function NodeStatusIndicator({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn("inline-flex h-5 items-center gap-1 rounded-sm border border-edge px-1.5 t-label-xs", className)}>{children}</span>;
}

export function NodeTooltip({ children, content }: { children: ReactNode; content: ReactNode }) {
  return <Tooltip><TooltipTrigger asChild><div>{children}</div></TooltipTrigger><TooltipContent className="max-w-xs">{content}</TooltipContent></Tooltip>;
}

export function NodeAppendix({ children }: { children: ReactNode }) {
  return <div className="border-t border-edge-subtle bg-canvas px-4 py-2 t-mono-xs text-ink-subtle">{children}</div>;
}

export function NodeSearch({ nodes, className }: { nodes: Node[]; className?: string }) {
  const [query, setQuery] = useState("");
  const { fitView } = useReactFlow();
  const matches = query.trim()
    ? nodes.filter((node) => String((node.data as { label?: string }).label ?? node.id).toLowerCase().includes(query.toLowerCase())).slice(0, 6)
    : [];
  return <div className={cn("relative w-[200px]",className)}>
    <Search className="pointer-events-none absolute left-2 top-2 h-3 w-3 text-ink-subtle" />
    <Input aria-label="Search workflow nodes" value={query} onChange={(event) => setQuery(event.target.value)} className="h-11 bg-canvas pl-7 t-mono-sm md:h-7" placeholder="Find a stage…" />
    {matches.length > 0 && <div className="absolute left-0 right-0 top-8 z-50 border border-edge-strong bg-surface p-1 shadow-overlay">{matches.map((node) => {const label=String((node.data as { label?: string }).label ?? node.id);const start=label.toLowerCase().indexOf(query.toLowerCase());return <button key={node.id} className="block h-7 w-full truncate px-2 text-left t-mono-sm hover:bg-hover" onClick={() => { void fitView({ nodes: [{ id: node.id }], duration: 300, maxZoom: 1.35 }); setQuery(""); }}>{start>=0?<>{label.slice(0,start)}<span className="text-brand-text">{label.slice(start,start+query.length)}</span>{label.slice(start+query.length)}</>:label}</button>})}</div>}
  </div>;
}

export function ZoomSlider({className}:{className?:string}={}) {
  const { zoom } = useViewport();
  const { zoomTo } = useReactFlow();
  return <label className={cn("flex h-7 items-center gap-2 border border-edge bg-canvas px-2 t-mono-xs text-ink-subtle",className)}>
    <span className="sr-only">Canvas zoom</span>
    <input aria-label="Canvas zoom" type="range" min="0.75" max="2" step="0.05" value={zoom} onChange={(event) => void zoomTo(Number(event.target.value), { duration: 120 })} className="w-20 accent-brand" />
    <span className="w-8 text-right">{Math.round(zoom * 100)}%</span>
  </label>;
}

export function WorkflowDevTools() {
  const nodeCount = useStore((state) => state.nodes.length);
  const edgeCount = useStore((state) => state.edges.length);
  if (!import.meta.env.DEV) return null;
  return <Panel position="bottom-left" className="border border-dashed border-edge bg-surface px-2 py-1 t-mono-xs text-ink-subtle">DEV · {nodeCount} nodes · {edgeCount} edges</Panel>;
}

export { Position };
