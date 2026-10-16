import { useCallback, useEffect, useRef, useState } from "react";
import type { WorkflowConfig, RunStop } from "../../../lib/workflow-config";
import { toRunRequest } from "../../../lib/workflow-config";

export interface ServiceHealth {
  version: string;
  schema_version: number;
  capabilities: string[];
  active_runs: Array<{ run_id: string; case_id: string; status: string }>;
}

export interface RunEvent {
  seq: number;
  type: "run_start" | "phase" | "dashboard_updated" | "indexing_error" | "run_complete" | "error" | "stopped";
  run_id?: string;
  case_id?: string;
  phase?: string;
  status?: string;
  message?: string;
  result?: Record<string, unknown>;
  [key: string]: unknown;
}

const SERVICE_URL = (import.meta.env.VITE_PATCHBENCH_SERVICE_URL as string | undefined)?.replace(/\/$/, "") ?? "";

export function useWorkflowService() {
  const [health,setHealth]=useState<ServiceHealth|null>(null);
  const [probing,setProbing]=useState(true);
  const [runId,setRunId]=useState<string|null>(null);
  const [runStatus,setRunStatus]=useState<"idle"|"queued"|"running"|"complete"|"error">("idle");
  const [events,setEvents]=useState<RunEvent[]>([]);
  const streamAbort=useRef<AbortController|null>(null);

  const probe=useCallback(async()=>{
    const controller=new AbortController();const timer=window.setTimeout(()=>controller.abort(),900);
    try{const response=await fetch(`${SERVICE_URL}/api/health`,{signal:controller.signal});if(!response.ok)throw new Error("service unavailable");setHealth(await response.json() as ServiceHealth)}catch{setHealth(null)}finally{window.clearTimeout(timer);setProbing(false)}
  },[]);
  useEffect(()=>{void probe();return()=>streamAbort.current?.abort()},[probe]);

  const attach=useCallback(async(id:string)=>{
    streamAbort.current?.abort();const controller=new AbortController();streamAbort.current=controller;
    try{
      const response=await fetch(`${SERVICE_URL}/api/runs/${encodeURIComponent(id)}/events`,{signal:controller.signal});
      if(!response.ok||!response.body)throw new Error(`Event stream unavailable (${response.status})`);
      const reader=response.body.getReader();const decoder=new TextDecoder();let pending="";let lastSeq=-1;
      for(;;){const {done,value}=await reader.read();pending+=decoder.decode(value,{stream:!done});const lines=pending.split("\n");pending=lines.pop()??"";for(const line of lines){if(!line.trim())continue;const event=JSON.parse(line) as RunEvent;if(event.seq<=lastSeq)continue;lastSeq=event.seq;setEvents((current)=>current.some((item)=>item.seq===event.seq)?current:[...current,event]);if(event.type==="run_start")setRunStatus(event.status==="queued"?"queued":"running");if(event.type==="phase")setRunStatus("running");if(event.type==="run_complete"||event.type==="stopped")setRunStatus("complete");if(event.type==="error")setRunStatus("error")}if(done)break}
    }catch(error){if(!controller.signal.aborted){setRunStatus("error");setEvents((current)=>[...current,{seq:(current.at(-1)?.seq??-1)+1,type:"error",message:error instanceof Error?error.message:"Event stream disconnected"}])}}
  },[]);

  const start=useCallback(async(config:WorkflowConfig,stopAt:RunStop)=>{
    setEvents([]);setRunId(null);setRunStatus("queued");
    try{
      const response=await fetch(`${SERVICE_URL}/api/runs`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(toRunRequest(config,stopAt))});
      let payload:{run_id?:string;error?:string};try{payload=await response.json() as {run_id?:string;error?:string}}catch{throw new Error(`Run service returned an invalid response (${response.status})`)}
      if(!response.ok||!payload.run_id)throw new Error(payload.error??`Run request failed (${response.status})`);
      setRunId(payload.run_id);void attach(payload.run_id);return payload.run_id;
    }catch(error){setRunStatus("error");throw error}
  },[attach]);

  const reconnect=useCallback((id:string)=>{setRunId(id);setEvents([]);setRunStatus("running");void attach(id)},[attach]);
  const loadPrompt=useCallback(async(id:string,signal?:AbortSignal)=>{
    const response=await fetch(`${SERVICE_URL}/api/runs/${encodeURIComponent(id)}/prompt`,{signal,cache:"no-store"});
    let payload:{prompt?:string;error?:string};try{payload=await response.json() as {prompt?:string;error?:string}}catch{throw new Error(`Prompt service returned an invalid response (${response.status})`)}
    if(!response.ok||typeof payload.prompt!=="string")throw new Error(payload.error??`Prompt is unavailable (${response.status})`);
    return payload.prompt;
  },[]);
  const cancel=useCallback(async()=>{if(!runId)return;const response=await fetch(`${SERVICE_URL}/api/runs/${encodeURIComponent(runId)}/cancel`,{method:"POST"});if(!response.ok)throw new Error("The run could not be cancelled")},[runId]);
  return {health,probing,probe,runId,runStatus,events,start,reconnect,loadPrompt,cancel,serviceUrl:SERVICE_URL||window.location.origin};
}
