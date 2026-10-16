import { useCallback, useEffect, useRef, useState } from "react";

export interface OperationEvent {
  seq:number;
  type:"operation_start"|"operation_status"|"log"|"dashboard_updated"|"indexing_error"|"operation_complete"|"operation_error";
  status?:string;
  message?:string;
  result?:unknown;
  [key:string]:unknown;
}

const ACTIVE_OPERATION_KEY="patchbench.operation.active";

export function useOperationService() {
  const [operationId,setOperationId]=useState<string|null>(null);
  const [status,setStatus]=useState<"idle"|"queued"|"running"|"complete"|"error">("idle");
  const [events,setEvents]=useState<OperationEvent[]>([]);
  const [result,setResult]=useState<unknown>(null);
  const [error,setError]=useState<string|null>(null);
  const streamAbort=useRef<AbortController|null>(null);
  useEffect(()=>()=>streamAbort.current?.abort(),[]);

  const attach=useCallback(async(id:string)=>{
    streamAbort.current?.abort();const controller=new AbortController();streamAbort.current=controller;
    try{
      const response=await fetch(`/api/operations/${encodeURIComponent(id)}/events`,{signal:controller.signal});
      if(!response.ok||!response.body)throw new Error(`Operation stream unavailable (${response.status})`);
      const reader=response.body.getReader();const decoder=new TextDecoder();let pending="";let lastSeq=-1;
      for(;;){const{done,value}=await reader.read();pending+=decoder.decode(value,{stream:!done});const lines=pending.split("\n");pending=lines.pop()??"";for(const line of lines){if(!line.trim())continue;const event=JSON.parse(line) as OperationEvent;if(event.seq<=lastSeq)continue;lastSeq=event.seq;setEvents((current)=>[...current,event]);if(event.type==="operation_status")setStatus("running");if(event.type==="operation_complete"){setResult(event.result);setStatus("complete");try{localStorage.removeItem(ACTIVE_OPERATION_KEY)}catch{/* Optional recovery state. */}}if(event.type==="operation_error"){setError(event.message??"Operation failed");setStatus("error");try{localStorage.removeItem(ACTIVE_OPERATION_KEY)}catch{/* Optional recovery state. */}}}if(done)break}
    }catch(reason){if(!controller.signal.aborted){setError(reason instanceof Error?reason.message:"Operation stream disconnected");setStatus("error");try{localStorage.removeItem(ACTIVE_OPERATION_KEY)}catch{/* Optional recovery state. */}}}
  },[]);

  const start=useCallback(async(payload:Record<string,unknown>)=>{
    setEvents([]);setResult(null);setError(null);setOperationId(null);setStatus("queued");
    try{
      const response=await fetch("/api/operations",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
      let body:{operation_id?:string;error?:string};try{body=await response.json() as typeof body}catch{throw new Error(`Operation service returned an invalid response (${response.status})`)}
      if(!response.ok||!body.operation_id)throw new Error(body.error??`Operation request failed (${response.status})`);
      setOperationId(body.operation_id);try{localStorage.setItem(ACTIVE_OPERATION_KEY,body.operation_id)}catch{/* Optional recovery state. */}void attach(body.operation_id);return body.operation_id;
    }catch(reason){setError(reason instanceof Error?reason.message:"Operation request failed");setStatus("error");throw reason}
  },[attach]);

  const reconnect=useCallback((id:string)=>{setOperationId(id);setEvents([]);setResult(null);setError(null);setStatus("running");void attach(id)},[attach]);
  useEffect(()=>{try{const active=localStorage.getItem(ACTIVE_OPERATION_KEY);if(active)reconnect(active)}catch{/* Optional recovery state. */}},[reconnect]);
  return{operationId,status,events,result,error,start,reconnect};
}
