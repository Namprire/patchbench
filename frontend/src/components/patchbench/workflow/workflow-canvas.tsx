import { useEffect, useMemo } from "react";
import { useReducedMotion } from "framer-motion";
import {
  Background, BackgroundVariant, Controls, MiniMap, Panel, ReactFlow, useEdgesState,
  useNodesState, type Edge, type EdgeTypes, type Node, type NodeTypes,
} from "@xyflow/react";
import type { Workflow, WorkflowNodeData } from "../../../types/dashboard";
import { WorkflowDevTools } from "./react-flow-ui";
import { WorkflowEdge } from "./workflow-edge";
import { WorkflowNode } from "./workflow-node";
import { WorkflowToolbar } from "./workflow-toolbar";
import { WORKFLOW_SCHEMA_VERSION } from "./workflow-layout";
import { useResolvedTheme } from "../theme-toggle";
import { useViewportTier } from "../../../lib/use-viewport-tier";

const nodeTypes:NodeTypes={workflowNode:WorkflowNode};
const edgeTypes:EdgeTypes={workflowEdge:WorkflowEdge};

function validateGraph(workflow:Workflow,nodes:Node[],edges:Edge[]){if(workflow.schema_version!==WORKFLOW_SCHEMA_VERSION)throw new Error(`Unsupported workflow schema ${workflow.schema_version}; this console supports ${WORKFLOW_SCHEMA_VERSION}.`);const ids=new Set<string>();for(const node of nodes){if(ids.has(node.id))throw new Error(`Duplicate workflow node ID: ${node.id}`);ids.add(node.id)}for(const edge of edges){if(!ids.has(edge.source)||!ids.has(edge.target))throw new Error(`Workflow edge ${edge.id} targets an absent node.`)}}

export function WorkflowCanvas({workflow,initialNodes,initialEdges,onSelect}:{workflow:Workflow;initialNodes:Node[];initialEdges:Edge[];mode:"architecture"|"trace";caseId?:string;runId?:string;selectedId:string|null;onSelect:(node:WorkflowNodeData|null)=>void}){
  const reducedMotion=useReducedMotion(); const resolvedTheme=useResolvedTheme();
  const tier=useViewportTier();const narrow=tier==="xs"||tier==="sm";const fitPadding=narrow ? 0.12 : 0.08;
  const preparedEdges=useMemo(()=>initialEdges.map((edge)=>({...edge,animated:reducedMotion?false:edge.animated})),[initialEdges,reducedMotion]);
  validateGraph(workflow,initialNodes,preparedEdges);
  const [nodes,setNodes,onNodesChange]=useNodesState(initialNodes);
  const [edges,setEdges,onEdgesChange]=useEdgesState(preparedEdges);
  useEffect(()=>{setNodes(initialNodes);setEdges(preparedEdges)},[initialNodes,preparedEdges,setEdges,setNodes]);
  return <div data-testid="workflow-canvas" data-nodes-draggable="false" data-nodes-connectable="false" className="workflow-canvas relative h-[320px] min-h-[320px] overflow-hidden bg-canvas sm:h-[380px] sm:min-h-[380px] md:h-[440px] md:min-h-[440px] lg:h-[clamp(480px,calc(100vh-420px),900px)] lg:min-h-[480px]">
    <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onNodeClick={(_,node)=>{const data=node.data as unknown as WorkflowNodeData;if(node.type==="workflowNode")onSelect(data)}} onPaneClick={()=>onSelect(null)} nodesDraggable={false} nodesConnectable={false} elementsSelectable edgesReconnectable={false} deleteKeyCode={null} fitView fitViewOptions={{padding:fitPadding,minZoom:.75,maxZoom:1}} colorMode={resolvedTheme} minZoom={.75} maxZoom={2}>
      <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="hsl(var(--dot))"/>
      <Controls showInteractive={false} className="[&_button]:!h-11 [&_button]:!w-11 md:[&_button]:!h-8 md:[&_button]:!w-8"/><MiniMap pannable zoomable nodeColor={(node)=>{const data=node.data as {frontier?:boolean;status?:string};return data.status==="error"?"hsl(var(--danger))":data.frontier?"hsl(var(--accent))":"hsl(var(--border-default))"}} maskColor="hsl(var(--bg-canvas) / .72)" className="!hidden !h-[120px] !w-[160px] !border !border-edge !bg-surface xl:!block"/>
      <Panel position="top-left"><WorkflowToolbar nodes={nodes}/></Panel>
      <WorkflowDevTools/>
    </ReactFlow>
  </div>;
}
