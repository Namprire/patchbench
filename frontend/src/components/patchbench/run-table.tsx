import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { type ColumnDef, type PaginationState, type SortingState, flexRender, getCoreRowModel, getPaginationRowModel, getSortedRowModel, useReactTable, type VisibilityState } from "@tanstack/react-table";
import { ArrowUpDown, Columns3, Copy, ExternalLink, FileDiff, GitCompareArrows, MoreHorizontal, Network } from "lucide-react";
import { toast } from "sonner";
import type { RunSummary } from "../../types/dashboard";
import { useComparison } from "../../data/dashboard";
import { compactLabel, formatCost, formatDuration, formatNumber, cn } from "../../lib/utils";
import { runContextLabel } from "../../lib/run-category";
import { Button } from "../ui/button";
import { Checkbox } from "../ui/checkbox";
import { DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "../ui/dropdown-menu";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../ui/table";
import { ToggleGroup, ToggleGroupItem } from "../ui/toggle-group";
import { EvaluationBadge, RunStatusBadge, TerminationBadge } from "./status-badges";
import { EmptyState } from "./empty-state";
import { useViewportTier, type ViewportTier } from "../../lib/use-viewport-tier";

type TableMode = "browse" | "compare";
type Density = "dense" | "comfortable";
type Props = { runs: RunSummary[]; pageSize?: number; compact?: boolean; hideControls?: boolean; initialSort?: string; density?: Density; onDensityChange?: (density: Density) => void; onClearFilters?: () => void };
const compactVisibility = { agent: false, harness: false, prompt: false, maxTurns: false, turns: false, activity: false, failure: false, input: false, output: false, cost: false, overlap: false };
const browseVisibility = { harness: false, prompt: false, maxTurns: false, turns: false, activity: false, failure: false, input: false, output: false, cost: false, overlap: false };
const narrowVisibility = { ...browseVisibility, case_id:false, agent:false, model:false, termination:false, patch:false, evaluation:false, runtime:false, actions:false };
function responsiveVisibility(tier:ViewportTier):VisibilityState {
  if(tier==="xl")return browseVisibility;
  if(tier==="lg")return {...narrowVisibility,case_id:true,model:true,termination:true,evaluation:true,agent:true,runtime:true,actions:true};
  if(tier==="md")return {...narrowVisibility,case_id:true,model:true,termination:true,evaluation:true};
  if(tier==="sm")return {...narrowVisibility,case_id:true,termination:true};
  return narrowVisibility;
}
const sortIds: Record<string,string> = { runtime:"runtime", inputTokens:"input", outputTokens:"output", cost:"cost" };
const isInteractiveTarget = (target: EventTarget | null) => target instanceof Element && Boolean(target.closest("a,button,input,[role=menuitem],[role=checkbox]"));

export function RunTable({ runs, pageSize = 12, compact = false, hideControls = false, initialSort, density = "comfortable", onDensityChange, onClearFilters }: Props) {
  const comparison = useComparison();
  const navigate = useNavigate();
  const tier=useViewportTier();
  const explicitColumns=useRef(localStorage.getItem("patchbench.runs.columns")!==null);
  const [mode,setMode]=useState<TableMode>(()=>hideControls?"browse":localStorage.getItem("patchbench.runs.mode")==="compare"?"compare":"browse");
  const [sorting, setSorting] = useState<SortingState>(()=>initialSort&&sortIds[initialSort]?[{id:sortIds[initialSort],desc:true}]:[]);
  const [columnVisibility, setColumnVisibility] = useState<VisibilityState>(()=>{
    if(compact)return tier==="xl"?compactVisibility:responsiveVisibility(tier);
    try{const stored=localStorage.getItem("patchbench.runs.columns");return stored?JSON.parse(stored) as VisibilityState:responsiveVisibility(tier)}catch{return responsiveVisibility(tier)}
  });
  const responsivePageSize=pageSize===12?(tier==="xs"?8:tier==="sm"?10:12):pageSize;
  const [pagination, setPagination] = useState<PaginationState>({ pageIndex: 0, pageSize:responsivePageSize });
  useEffect(()=>{if(!hideControls)localStorage.setItem("patchbench.runs.mode",mode)},[hideControls,mode]);
  useEffect(()=>{if(compact)setColumnVisibility(tier==="xl"?compactVisibility:responsiveVisibility(tier));else if(!explicitColumns.current)setColumnVisibility(responsiveVisibility(tier))},[compact,tier]);
  useEffect(()=>setPagination((current)=>({...current,pageSize:responsivePageSize,pageIndex:0})),[responsivePageSize]);
  useEffect(()=>setPagination((current)=>({...current,pageIndex:0})),[runs]);
  useEffect(()=>setSorting(initialSort&&sortIds[initialSort]?[{id:sortIds[initialSort],desc:true}]:[]),[initialSort]);

  const toggleComparison=useCallback((run:RunSummary)=>{
    const selected=comparison.contains(run.case_id,run.run_id);
    if(!selected&&comparison.selected.length>=4){toast.error("Comparison is limited to four runs");return}
    comparison.toggle(run.case_id,run.run_id);
    toast.success(selected?"Removed from comparison":"Added to comparison");
  },[comparison]);
  const openRun=(run:RunSummary)=>navigate(`/runs/${encodeURIComponent(run.case_id)}/${encodeURIComponent(run.run_id)}?tab=workflow`);
  const columns = useMemo<ColumnDef<RunSummary>[]>(() => [
    ...(mode==="compare"?[{ id: "compare", enableHiding:false, header: "", cell: ({row}: {row:{original:RunSummary}}) => <label className="flex h-11 w-11 items-center justify-center md:h-7 md:w-7"><Checkbox aria-label={`Compare ${row.original.run_id}`} checked={comparison.contains(row.original.case_id, row.original.run_id)} onCheckedChange={() => toggleComparison(row.original)} /></label> } as ColumnDef<RunSummary>]:[]),
    { accessorKey: "case_id", header: "Case", cell: ({row}) => <span className="whitespace-nowrap font-mono text-xs">{row.original.case_id}</span> },
    { accessorKey: "run_id", header: ({column}) => <button className={cn("flex min-h-11 items-center gap-1 rounded-sm t-label focus-visible:outline-none md:min-h-7",column.getIsSorted()&&"text-ink")} onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}>Run ID<ArrowUpDown className={cn("h-3 w-3",column.getIsSorted()&&"text-brand")} /></button>, cell: ({row}) => <div className="max-w-64"><Link className="inline-flex min-h-11 min-w-11 max-w-full items-center truncate t-mono-sm font-medium underline-offset-4 hover:underline sm:min-h-0 sm:min-w-0" to={`/runs/${encodeURIComponent(row.original.case_id)}/${encodeURIComponent(row.original.run_id)}?tab=workflow`}>{row.original.run_id}</Link><p className={cn("truncate t-mono-xs text-ink-subtle sm:mt-0.5 sm:hidden",row.original.historical&&"border-l-2 border-l-warn pl-1")}>{row.original.case_id} · {row.original.actual_model??row.original.requested_model??"unknown"} · {compactLabel(row.original.termination_reason)}</p>{runContextLabel(row.original)&&<p className={cn("mt-0.5 hidden truncate t-mono-xs text-ink-subtle sm:block",row.original.historical&&"border-l-2 border-l-warn pl-1")}>{runContextLabel(row.original)}</p>}</div> },
    { id: "agent", accessorKey: "agent", header: "Agent", cell: ({row}) => row.original.agent ?? "—" },
    { id: "harness", accessorKey: "agent_harness", header: "Harness", cell: ({row}) => <span className="whitespace-nowrap font-mono text-xs">{row.original.agent_harness ?? "—"}</span> },
    { id: "model", accessorKey: "actual_model", header: "Model", cell: ({row}) => <span className="whitespace-nowrap font-mono text-xs">{row.original.actual_model ?? "unknown"}</span> },
    { id: "prompt", accessorKey: "prompt_mode", header: "Prompt", cell: ({row}) => <span className="font-mono text-xs">{row.original.prompt_mode?.replaceAll("_", " ") ?? "—"}</span> },
    { id: "maxTurns", accessorKey: "configured_max_turns", header: "Max turns", cell: ({row}) => <span className="block text-right t-mono-sm">{formatNumber(row.original.configured_max_turns)}</span> },
    { id: "turns", accessorKey: "reported_turns", header: "Reported", cell: ({row}) => <span className="block text-right t-mono-sm">{formatNumber(row.original.reported_turns)}</span> },
    { id: "activity", accessorKey: "activity_observed", header: "Activity", cell: ({row}) => row.original.activity_observed == null ? "—" : row.original.activity_observed ? "yes" : "no" },
    { id: "termination", accessorKey: "termination_reason", header: "Termination", cell: ({row}) => <TerminationBadge reason={row.original.termination_reason} /> },
    { id: "patch", accessorKey: "patch_captured", header: "Patch", cell: ({row}) => row.original.patch_captured ? row.original.partial_patch_captured ? "partial" : "captured" : "none" },
    { id: "evaluation", header: "Evaluation", cell: ({row}) => <EvaluationBadge skipped={row.original.evaluation_skipped} applied={row.original.patch_applied} resolved={row.original.resolved} /> },
    { id: "resolved", accessorKey: "resolved", header: "Outcome", cell: ({row}) => <RunStatusBadge resolved={row.original.resolved} /> },
    { id: "failure", accessorKey: "failure_mode", header: "Failure mode", cell: ({row}) => row.original.failure_mode?.replaceAll("_", " ") ?? "—" },
    { id: "runtime", accessorKey: "runtime_seconds", header: "Runtime", cell: ({row}) => <span className="block text-right t-mono-sm">{formatDuration(row.original.runtime_seconds)}</span> },
    { id: "input", accessorKey: "input_tokens", header: "Input", cell: ({row}) => <span className="block text-right t-mono-sm">{formatNumber(row.original.input_tokens)}</span> },
    { id: "output", accessorKey: "output_tokens", header: "Output", cell: ({row}) => <span className="block text-right t-mono-sm">{formatNumber(row.original.output_tokens)}</span> },
    { id: "cost", accessorKey: "reported_cost_usd", header: "Cost", cell: ({row}) => <span className="block text-right t-mono-sm">{formatCost(row.original.reported_cost_usd)}</span> },
    { id: "overlap", header: "Overlap", cell: ({row}) => <span className="block text-right t-mono-sm">{row.original.patch_comparison ? row.original.patch_comparison.file_overlap_jaccard.toFixed(2) : "—"}</span> },
    { id: "actions", enableHiding:true, header: "", cell: ({row}) => {
      const run=row.original; const detail=`/runs/${encodeURIComponent(run.case_id)}/${encodeURIComponent(run.run_id)}`;
      return <DropdownMenu><DropdownMenuTrigger asChild><Button aria-label={`Actions for ${run.run_id}`} variant="ghost" size="icon" className="h-8 w-8"><MoreHorizontal className="h-4 w-4" /></Button></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuItem asChild><Link to={detail}><ExternalLink className="mr-2 h-4 w-4" />Open run</Link></DropdownMenuItem><DropdownMenuItem onSelect={() => toggleComparison(run)}><GitCompareArrows className="mr-2 h-4 w-4" />{comparison.contains(run.case_id,run.run_id)?"Remove from compare":"Add to compare"}</DropdownMenuItem><DropdownMenuItem onSelect={() => { void navigator.clipboard.writeText(run.run_id); toast.success("Run ID copied"); }}><Copy className="mr-2 h-4 w-4" />Copy run ID</DropdownMenuItem>{run.patch_captured&&<DropdownMenuItem asChild><Link to={`${detail}?tab=patch`}><FileDiff className="mr-2 h-4 w-4" />View patch</Link></DropdownMenuItem>}<DropdownMenuItem asChild><Link to={`${detail}?tab=workflow`}><Network className="mr-2 h-4 w-4" />View execution trace</Link></DropdownMenuItem></DropdownMenuContent></DropdownMenu>;
    } },
  ], [comparison, mode, toggleComparison]);
  const table = useReactTable({ data: runs, columns, state: { sorting, columnVisibility, pagination }, onSortingChange: setSorting, onColumnVisibilityChange: (updater)=>{explicitColumns.current=true;setColumnVisibility((current)=>{const next=typeof updater==="function"?updater(current):updater;if(!compact)localStorage.setItem("patchbench.runs.columns",JSON.stringify(next));return next})}, onPaginationChange: setPagination, getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel(), getPaginationRowModel: getPaginationRowModel() });
  const handleRowClick=(event:MouseEvent<HTMLTableRowElement>,run:RunSummary)=>{if(isInteractiveTarget(event.target))return;if(mode==="browse")openRun(run);else toggleComparison(run)};
  const handleRowKey=(event:KeyboardEvent<HTMLTableRowElement>,run:RunSummary)=>{if(event.key!=="Enter"&&event.key!==" ")return;if(isInteractiveTarget(event.target))return;event.preventDefault();if(mode==="browse")openRun(run);else toggleComparison(run)};
  if (!runs.length) return <div className="border border-edge-strong bg-surface"><EmptyState className="border-0" title="No runs match these filters." description="Clear filters or change the search terms." />{onClearFilters&&<div className="flex justify-center pb-6"><Button variant="outline" size="sm" onClick={onClearFilters}>Clear filters</Button></div>}</div>;
  return <div className="space-y-3">{!hideControls&&<div className="flex flex-wrap items-center justify-between gap-2"><ToggleGroup type="single" value={mode} onValueChange={(value)=>value&&setMode(value as TableMode)} aria-label="Run table mode"><ToggleGroupItem value="browse">Browse</ToggleGroupItem><ToggleGroupItem value="compare">Compare</ToggleGroupItem></ToggleGroup><div className="flex flex-wrap items-center justify-end gap-2"><DropdownMenu><DropdownMenuTrigger asChild><Button variant="outline" size="sm"><Columns3 className="h-3.5 w-3.5" />Columns</Button></DropdownMenuTrigger><DropdownMenuContent align="end">{table.getAllColumns().filter((column) => column.getCanHide()&&column.id!=="actions").map((column) => <DropdownMenuCheckboxItem key={column.id} checked={column.getIsVisible()} onCheckedChange={(value) => column.toggleVisibility(Boolean(value))}>{column.id}</DropdownMenuCheckboxItem>)}</DropdownMenuContent></DropdownMenu>{onDensityChange&&<ToggleGroup type="single" value={density} onValueChange={(value)=>value&&onDensityChange(value as Density)} aria-label="Table density"><ToggleGroupItem value="comfortable">Comfortable</ToggleGroupItem><ToggleGroupItem value="dense">Dense</ToggleGroupItem></ToggleGroup>}</div></div>}<div className="border-y border-edge-strong bg-surface md:border-x"><Table><TableHeader>{table.getHeaderGroups().map((group) => <TableRow key={group.id}>{group.headers.map((header) => <TableHead key={header.id}>{header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}</TableHead>)}</TableRow>)}</TableHeader><TableBody>{table.getRowModel().rows.map((row) => {const selected=comparison.contains(row.original.case_id,row.original.run_id);return <TableRow key={row.id} tabIndex={0} aria-label={`${mode==="browse"?"Open":"Select"} run ${row.original.run_id}`} aria-selected={mode==="compare"?selected:undefined} onClick={(event)=>handleRowClick(event,row.original)} onKeyDown={(event)=>handleRowKey(event,row.original)} className={cn("cursor-pointer outline-none focus-visible:bg-pressed [&_td]:h-11 [&_td]:py-0 md:[&_td]:h-9",density==="dense"&&"md:[&_td]:h-7",selected&&"border-l-2 border-l-brand bg-chosen hover:bg-chosen")}>{row.getVisibleCells().map((cell) => <TableCell key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</TableCell>)}</TableRow>})}</TableBody></Table></div><div className="flex items-center justify-between gap-3 t-mono-xs text-ink-subtle"><span>{runs.length} runs · page {table.getState().pagination.pageIndex + 1} of {table.getPageCount()}</span><div className="flex gap-2"><Button variant="outline" size="sm" disabled={!table.getCanPreviousPage()} onClick={() => table.previousPage()}>Previous</Button><Button variant="outline" size="sm" disabled={!table.getCanNextPage()} onClick={() => table.nextPage()}>Next</Button></div></div></div>;
}
