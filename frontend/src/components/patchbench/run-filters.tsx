import { ChevronDown, Search, SlidersHorizontal, X } from "lucide-react";
import type { RunSummary } from "../../types/dashboard";
import { deriveRunCategory, runCategoryLabels, type RunCategory } from "../../lib/run-category";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "../ui/collapsible";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "../ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "../ui/popover";

export interface RunFilterState {
  search: string; caseId: string; model: string; harness: string; promptMode: string;
  category: string; termination: string; evaluation: string; resolved: string;
  partial: string; patchApplied: string;
}
export const emptyRunFilters: RunFilterState = {
  search:"", caseId:"all", model:"all", harness:"all", promptMode:"all", category:"all",
  termination:"all", evaluation:"all", resolved:"all", partial:"all", patchApplied:"all",
};
const unique=(runs:RunSummary[],select:(run:RunSummary)=>string|null)=>[...new Set(runs.map(select).filter((value):value is string=>Boolean(value)))].sort();

export function filterRuns(runs:RunSummary[],filters:RunFilterState):RunSummary[] {
  const term=filters.search.toLowerCase().trim();
  return runs.filter((run)=>{
    const searchable=[run.case_id,run.run_id,run.agent,run.agent_harness,run.actual_model,run.requested_model,run.provider,run.prompt_mode,run.failure_mode].join(" ").toLowerCase();
    return (!term||searchable.includes(term))
      &&(filters.caseId==="all"||run.case_id===filters.caseId)
      &&(filters.model==="all"||run.actual_model===filters.model)
      &&(filters.harness==="all"||run.agent_harness===filters.harness)
      &&(filters.promptMode==="all"||run.prompt_mode===filters.promptMode)
      &&(filters.category==="all"||deriveRunCategory(run)===filters.category)
      &&(filters.termination==="all"||run.termination_reason===filters.termination)
      &&(filters.evaluation==="all"||(filters.evaluation==="skipped"?run.evaluation_skipped:!run.evaluation_skipped))
      &&(filters.resolved==="all"||run.resolved===(filters.resolved==="yes"))
      &&(filters.partial==="all"||run.partial_patch_captured===(filters.partial==="yes"))
      &&(filters.patchApplied==="all"||run.patch_applied===(filters.patchApplied==="yes"));
  });
}

function FilterSelect({label,value,values,onChange}:{label:string;value:string;values:string[];onChange:(value:string)=>void}) {
  return <Select value={value} onValueChange={onChange}><SelectTrigger className="w-auto min-w-36 font-mono md:h-8"><SelectValue placeholder={label}/></SelectTrigger><SelectContent><SelectItem value="all">All {label}</SelectItem>{values.map((item)=><SelectItem key={item} value={item}>{item.replaceAll("_"," ")}</SelectItem>)}</SelectContent></Select>;
}
function SearchFilter({label,value,values,onChange}:{label:string;value:string;values:string[];onChange:(value:string)=>void}) {
  return <Popover><PopoverTrigger asChild><Button variant="outline" size="sm" className="min-w-36 justify-between font-mono font-normal md:h-8">{value==="all"?`All ${label}`:value}<ChevronDown className="h-3.5 w-3.5 text-ink-subtle"/></Button></PopoverTrigger><PopoverContent className="p-0" align="start"><Command><CommandInput placeholder={`Search ${label}…`}/><CommandList><CommandEmpty>No {label} found.</CommandEmpty><CommandGroup><CommandItem onSelect={()=>onChange("all")}>All {label}</CommandItem>{values.map((item)=><CommandItem key={item} value={item} onSelect={()=>onChange(item)}>{item}</CommandItem>)}</CommandGroup></CommandList></Command></PopoverContent></Popover>;
}
function BooleanSelect({value,onChange,all,yes,no}:{value:string;onChange:(value:string)=>void;all:string;yes:string;no:string}) {
  return <Select value={value} onValueChange={onChange}><SelectTrigger className="w-auto min-w-36 font-mono md:h-8"><SelectValue/></SelectTrigger><SelectContent><SelectItem value="all">{all}</SelectItem><SelectItem value="yes">{yes}</SelectItem><SelectItem value="no">{no}</SelectItem></SelectContent></Select>;
}

const chipNames:Record<keyof RunFilterState,string>={search:"Search",caseId:"Case",model:"Model",harness:"Harness",promptMode:"Prompt",category:"Category",termination:"Termination",evaluation:"Evaluation",resolved:"Outcome",partial:"Partial patch",patchApplied:"Patch applied"};
export function RunFilters({runs,value,onChange,resultCount}:{runs:RunSummary[];value:RunFilterState;onChange:(value:RunFilterState)=>void;resultCount?:number}) {
  const set=(key:keyof RunFilterState)=>(next:string)=>onChange({...value,[key]:next});
  const active=(Object.keys(value) as (keyof RunFilterState)[]).filter((key)=>value[key]!=="all"&&value[key]!=="");
  const clearOne=(key:keyof RunFilterState)=>onChange({...value,[key]:key==="search"?"":"all"});
  return <Collapsible className="border border-edge bg-surface p-3">
    <div className="flex flex-wrap items-center gap-2">
      <div className="relative min-w-64 flex-1"><Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-subtle"/><Input aria-label="Search runs" value={value.search} onChange={(event)=>set("search")(event.target.value)} placeholder="Search case, run, model, failure…" className="pl-9 md:h-8"/></div>
      <FilterSelect label="cases" value={value.caseId} values={unique(runs,(run)=>run.case_id)} onChange={set("caseId")}/>
      <SearchFilter label="models" value={value.model} values={unique(runs,(run)=>run.actual_model)} onChange={set("model")}/>
      <Select value={value.category} onValueChange={set("category")}><SelectTrigger className="w-auto min-w-36 md:h-8"><SelectValue/></SelectTrigger><SelectContent><SelectItem value="all">All categories</SelectItem>{(["benchmark","sanity","diagnostic","synthetic","unknown"] as RunCategory[]).map((item)=><SelectItem key={item} value={item}>{runCategoryLabels[item]}</SelectItem>)}</SelectContent></Select>
      <CollapsibleTrigger asChild><Button variant="outline" size="sm" className="md:h-8"><SlidersHorizontal className="h-3.5 w-3.5"/>More</Button></CollapsibleTrigger>
      <Button variant="ghost" size="sm" className="md:h-8" disabled={!active.length} onClick={()=>onChange(emptyRunFilters)}><X className="h-3.5 w-3.5"/>Clear filters</Button>
      {resultCount!=null&&<span className={cn("ml-auto whitespace-nowrap t-mono-sm text-ink-subtle",resultCount===0&&"text-warn")}>{resultCount} results</span>}
    </div>
    <CollapsibleContent className="mt-3 flex flex-wrap gap-2 border-t border-edge-subtle pt-3 data-[state=closed]:hidden">
      <FilterSelect label="harnesses" value={value.harness} values={unique(runs,(run)=>run.agent_harness)} onChange={set("harness")}/>
      <FilterSelect label="prompt modes" value={value.promptMode} values={unique(runs,(run)=>run.prompt_mode)} onChange={set("promptMode")}/>
      <FilterSelect label="termination" value={value.termination} values={unique(runs,(run)=>run.termination_reason)} onChange={set("termination")}/>
      <Select value={value.evaluation} onValueChange={set("evaluation")}><SelectTrigger className="w-auto min-w-36"><SelectValue/></SelectTrigger><SelectContent><SelectItem value="all">All evaluations</SelectItem><SelectItem value="evaluated">Evaluated</SelectItem><SelectItem value="skipped">Skipped</SelectItem></SelectContent></Select>
      <BooleanSelect value={value.resolved} onChange={set("resolved")} all="All outcomes" yes="Resolved" no="Unresolved"/>
      <BooleanSelect value={value.partial} onChange={set("partial")} all="All patch states" yes="Partial patch" no="Not partial"/>
      <BooleanSelect value={value.patchApplied} onChange={set("patchApplied")} all="All apply states" yes="Patch applied" no="Patch not applied"/>
    </CollapsibleContent>
    {active.length>0&&<div aria-label="Active filters" className="mt-3 flex flex-wrap gap-2 border-t border-edge-subtle pt-3 md:gap-1.5">{active.map((key)=><button key={key} onClick={()=>clearOne(key)} className="inline-flex h-11 items-center gap-1 rounded-sm border border-brand-border bg-brand-soft px-2 t-label-xs text-brand-text outline-none transition-colors hover:border-brand hover:bg-chosen md:h-5" aria-label={`Remove ${chipNames[key]} filter`}><span>{chipNames[key]}: {value[key].replaceAll("_"," ")}</span><X className="h-3 w-3"/></button>)}</div>}
  </Collapsible>;
}
