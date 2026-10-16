import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";

export function CollapsibleJson({ label, value, showEmpty = false }: { label: string; value: unknown; showEmpty?: boolean }) {
  const [open,setOpen]=useState(false);
  if (value == null && !showEmpty) return null;
  const count=Array.isArray(value)?`${value.length} items`:value&&typeof value==="object"?`${Object.keys(value).length} keys`:value==null?"empty":"1 value";
  const Icon=open?ChevronDown:ChevronRight;
  return <div><button type="button" aria-expanded={open} onClick={()=>setOpen((current)=>!current)} className="flex h-7 w-full items-center gap-2 border border-edge bg-surface px-2 text-left outline-none hover:bg-hover focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"><Icon className="h-3 w-3"/><span className="t-label">{label}</span><span className="ml-auto t-mono-xs text-ink-subtle">{count}</span></button>{open&&<pre className="max-h-[360px] overflow-auto border border-t-0 border-edge bg-inset p-2 t-console text-ink">{JSON.stringify(value,null,2)}</pre>}</div>;
}
