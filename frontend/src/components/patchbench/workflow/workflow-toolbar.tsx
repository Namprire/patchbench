import type { Node } from "@xyflow/react";
import { NodeSearch, ZoomSlider } from "./react-flow-ui";

export function WorkflowToolbar({nodes}:{nodes:Node[]}){
  return <div className="flex w-[calc(100vw-64px)] flex-wrap items-center gap-2 border border-edge bg-surface p-2 sm:w-auto">
    <span className="px-1 t-label-xs text-ink-subtle">Flow →</span>
    <NodeSearch nodes={nodes} className="order-first w-full sm:w-[180px] md:order-none lg:w-[200px]"/>
    <ZoomSlider className="hidden md:flex"/>
  </div>;
}
