import { GitCompareArrows, X } from "lucide-react";
import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { useComparison } from "../../data/dashboard";
import { Button } from "../ui/button";
import { buttonVariants } from "../ui/button";
import { cn } from "../../lib/utils";
export function CompareTray() {
  const { selected, remove, clear } = useComparison();
  const trayRef=useRef<HTMLElement|null>(null);
  useEffect(()=>{const tray=trayRef.current;if(!tray){document.documentElement.style.removeProperty("--compare-tray-h");return}const update=()=>document.documentElement.style.setProperty("--compare-tray-h",`${tray.offsetHeight+16}px`);update();const observer=new ResizeObserver(update);observer.observe(tray);return()=>{observer.disconnect();document.documentElement.style.removeProperty("--compare-tray-h")}},[selected.length]);
  if (!selected.length) return null;
  const parsed=selected.map((item)=>{const split=item.indexOf("/");return {caseId:item.slice(0,split),runId:item.slice(split+1)}});
  return <aside ref={trayRef} aria-label="Run comparison selection" className="fixed bottom-2 left-1/2 z-40 w-[calc(100%-1rem)] max-w-3xl -translate-x-1/2 border border-edge-strong bg-surface px-3 py-2 md:bottom-4"><div className="flex flex-wrap items-center gap-2"><GitCompareArrows className="h-4 w-4 shrink-0" /><p className="mr-1 t-label">{selected.length} selected</p><div className="flex min-w-0 flex-1 flex-wrap gap-1.5">{parsed.map((item)=><span key={`${item.caseId}/${item.runId}`} className="inline-flex max-w-48 items-center gap-1 rounded-sm border border-edge bg-chosen pl-2 t-mono-xs"><span className="truncate">{item.runId}</span><button aria-label={`Remove ${item.runId} from comparison`} className="flex h-11 w-11 items-center justify-center rounded-sm hover:bg-hover focus-visible:outline-none md:h-5 md:w-5" onClick={()=>{remove(item.caseId,item.runId);toast.success("Removed from comparison")}}><X className="h-3 w-3" /></button></span>)}</div>{selected.length>=2?<Link className={cn(buttonVariants({ size: "sm" }), "shrink-0")} to="/compare">Compare</Link>:<Button size="sm" disabled>Compare</Button>}<Button aria-label="Clear comparison" variant="ghost" size="icon" onClick={()=>{clear();toast.success("Comparison cleared")}}><X className="h-4 w-4" /></Button></div>{selected.length<2&&<p className="mt-1 pl-6 t-mono-xs text-ink-subtle">Select one more run to compare.</p>}</aside>;
}
