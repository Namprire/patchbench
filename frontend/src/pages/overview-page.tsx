import { Activity, Banknote, Bot, Boxes, Clock3, FileWarning, Gauge, GitCompareArrows, ListChecks, Network, ScrollText, TerminalSquare } from "lucide-react";
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { EmptyState } from "../components/patchbench/empty-state";
import { MetricCard } from "../components/patchbench/metric-card";
import { MetricCluster } from "../components/patchbench/metric-cluster";
import { PipelineBand, type PipelineStep } from "../components/patchbench/pipeline-band";
import { RunTable } from "../components/patchbench/run-table";
import { Tabs, TabsList, TabsTrigger } from "../components/ui/tabs";
import { useDashboard } from "../data/dashboard";
import { deriveRunCategory, runCategoryLabels, type RunCategory } from "../lib/run-category";
import { formatCompactNumber, formatCost, formatDuration, formatNumber } from "../lib/utils";
import type { RunSummary } from "../types/dashboard";
import { PageError, PageLoading } from "./page-state";

const requiresReview = (run: RunSummary) =>
  (run.termination_reason === "max_turns" && run.partial_patch_captured) ||
  run.execution_detail === "timeout_after_activity" ||
  run.evaluation_skipped ||
  (run.historical && (!run.actual_model || run.actual_model === "claude-code-default")) ||
  (run.patch_captured && run.patch_applied === null);

type BenchmarkResult={agent:string;model:string;runs:number;evaluated:number;resolved:number;resolvedRate:number|null;averageCost:number|null;averageRuntime:number|null};

export type OverviewMetrics = {
  evaluated: number;
  skipped: number;
  evaluationUnknown: number;
  resolved: number;
  knownOutcomes: number;
  resolvedRate: number | null;
  patchesCaptured: number;
  patchesApplied: number;
  patchApplyRate: number | null;
  partialPatches: number;
  averageRuntime: number | null;
  runtimeCoverage: number;
  inputTokens: number;
  inputCoverage: number;
  outputTokens: number;
  outputCoverage: number;
  reportedCost: number;
  costCoverage: number;
};

const finiteValues = (runs: RunSummary[], select: (run: RunSummary) => number | null) =>
  runs.flatMap((run) => {
    const value = select(run);
    return value != null && Number.isFinite(value) ? [value] : [];
  });

export function calculateOverviewMetrics(runs: RunSummary[]): OverviewMetrics {
  const evaluated = runs.filter((run) => run.evaluation_skipped === false).length;
  const skipped = runs.filter((run) => run.evaluation_skipped === true).length;
  const evaluationUnknown = runs.length - evaluated - skipped;
  const knownOutcomeRuns = runs.filter((run) => typeof run.resolved === "boolean");
  const resolved = knownOutcomeRuns.filter((run) => run.resolved === true).length;
  const capturedRuns = runs.filter((run) => run.patch_captured === true);
  const patchesApplied = capturedRuns.filter((run) => run.patch_applied === true).length;
  const runtimes = finiteValues(runs, (run) => run.runtime_seconds);
  const inputs = finiteValues(runs, (run) => run.input_tokens);
  const outputs = finiteValues(runs, (run) => run.output_tokens);
  const costs = finiteValues(runs, (run) => run.reported_cost_usd);

  return {
    evaluated,
    skipped,
    evaluationUnknown,
    resolved,
    knownOutcomes: knownOutcomeRuns.length,
    resolvedRate: knownOutcomeRuns.length ? resolved / knownOutcomeRuns.length * 100 : null,
    patchesCaptured: capturedRuns.length,
    patchesApplied,
    patchApplyRate: capturedRuns.length ? patchesApplied / capturedRuns.length * 100 : null,
    partialPatches: runs.filter((run) => run.partial_patch_captured === true).length,
    averageRuntime: runtimes.length ? runtimes.reduce((sum, value) => sum + value, 0) / runtimes.length : null,
    runtimeCoverage: runtimes.length,
    inputTokens: inputs.reduce((sum, value) => sum + value, 0),
    inputCoverage: inputs.length,
    outputTokens: outputs.reduce((sum, value) => sum + value, 0),
    outputCoverage: outputs.length,
    reportedCost: costs.reduce((sum, value) => sum + value, 0),
    costCoverage: costs.length,
  };
}

const demoBenchmark:BenchmarkResult[]=[
  {agent:"agent-harness-a",model:"model-reasoning",runs:50,evaluated:50,resolved:39,resolvedRate:78,averageCost:.62,averageRuntime:312},
  {agent:"agent-harness-a",model:"model-coder",runs:50,evaluated:50,resolved:36,resolvedRate:72,averageCost:.44,averageRuntime:248},
  {agent:"agent-harness-b",model:"model-balanced",runs:50,evaluated:50,resolved:33,resolvedRate:66,averageCost:.28,averageRuntime:196},
  {agent:"custom-container",model:"local-model",runs:50,evaluated:50,resolved:29,resolvedRate:58,averageCost:.08,averageRuntime:171},
];

function CountUp({to,format,duration=900}:{to:number;format:(value:number)=>string;duration?:number}){
  const[value,setValue]=useState(0);
  useEffect(()=>{if(!Number.isFinite(to)){setValue(to);return}if(window.matchMedia("(prefers-reduced-motion: reduce)").matches){setValue(to);return}let frame=0;const started=performance.now();const update=(now:number)=>{const progress=Math.min((now-started)/duration,1);setValue(to*(1-Math.pow(1-progress,3)));if(progress<1)frame=requestAnimationFrame(update)};frame=requestAnimationFrame(update);return()=>cancelAnimationFrame(frame)},[duration,to]);
  return <span data-count-target={to}>{format(value)}</span>;
}

export function benchmarkResults(runs:RunSummary[]):{rows:BenchmarkResult[];demo:boolean}{
  const attributable=runs.filter((run)=>run.agent||run.agent_harness||run.actual_model||run.requested_model);if(!attributable.length)return{rows:demoBenchmark,demo:true};
  const groups=new Map<string,{agent:string;model:string;runs:RunSummary[]}>();
  for(const run of attributable){const agent=run.agent??run.agent_harness??"unattributed agent";const model=run.actual_model??run.requested_model??"unknown model";const key=`${agent}\u0000${model}`;const group=groups.get(key)??{agent,model,runs:[]};group.runs.push(run);groups.set(key,group)}
  const rows=[...groups.values()].map((group)=>{const evaluatedRuns=group.runs.filter((run)=>run.evaluation_skipped===false);const evaluated=evaluatedRuns.length;const resolved=evaluatedRuns.filter((run)=>run.resolved===true).length;const costs=group.runs.flatMap((run)=>run.reported_cost_usd==null?[]:[run.reported_cost_usd]);const runtimes=group.runs.flatMap((run)=>run.runtime_seconds==null?[]:[run.runtime_seconds]);return{agent:group.agent,model:group.model,runs:group.runs.length,evaluated,resolved,resolvedRate:evaluated?resolved/evaluated*100:null,averageCost:costs.length?costs.reduce((sum,value)=>sum+value,0)/costs.length:null,averageRuntime:runtimes.length?runtimes.reduce((sum,value)=>sum+value,0)/runtimes.length:null}});
  return{rows:rows.sort((a,b)=>(b.resolvedRate??-1)-(a.resolvedRate??-1)||b.runs-a.runs||a.model.localeCompare(b.model)),demo:false};
}

function BenchmarkDimensions(){return <div aria-label="Benchmark dimensions" className="grid border border-edge-strong bg-surface sm:grid-cols-4">{[["01","Frozen case","fixed commit + task"],["02","Agent","any harness"],["03","Model","requested + actual"],["04","Evaluator","same hidden tests"]].map(([index,label,detail],position)=><div key={label} className="benchmark-dimension relative border-b border-edge-subtle p-3 last:border-b-0 sm:border-b-0 sm:border-r sm:last:border-r-0" style={{"--agent-delay":`${position*90}ms`} as CSSProperties}><span className="t-label-xs text-brand">{index}</span><p className="mt-2 t-label text-ink">{label}</p><p className="mt-1 t-mono-xs text-ink-subtle">{detail}</p>{position<3&&<span aria-hidden="true" className="absolute -right-2 top-1/2 z-10 hidden h-4 w-4 -translate-y-1/2 items-center justify-center bg-surface t-mono-xs text-brand sm:flex">→</span>}</div>)}</div>}

function BenchmarkLeaderboard({rows,demo}:{rows:BenchmarkResult[];demo:boolean}){
  return <section aria-label="Benchmark results" className="overflow-hidden border border-edge-strong bg-surface"><header className="flex flex-wrap items-center justify-between gap-3 border-b border-edge-strong px-4 py-3"><div className="flex items-center gap-3"><div><h2 className="t-label text-ink">Benchmark results</h2><p className="mt-1 t-body-sm text-ink-muted">Ranked by resolved rate for each distinct agent and model pair.</p></div>{demo&&<span className="border border-warn bg-warn-soft px-1.5 py-1 t-label-xs text-warn">Illustrative demo</span>}</div><Link to="/compare" className="inline-flex min-h-9 items-center gap-2 bg-invert px-3 t-label text-invert-foreground transition-transform duration-2 hover:-translate-y-0.5"><GitCompareArrows className="h-3.5 w-3.5"/>Compare results</Link></header><div className="overflow-x-auto"><table className="w-full min-w-[760px] border-collapse"><thead><tr className="border-b border-edge bg-inset text-left"><th className="w-14 px-4 py-2 t-label-xs text-ink-subtle">#</th><th className="px-3 py-2 t-label-xs text-ink-subtle">Model</th><th className="px-3 py-2 t-label-xs text-ink-subtle">Agent</th><th className="w-52 px-3 py-2 t-label-xs text-ink-subtle">Resolved</th><th className="w-28 px-3 py-2 t-label-xs text-ink-subtle">Avg. cost</th><th className="w-28 px-3 py-2 t-label-xs text-ink-subtle">Avg. time</th><th className="w-20 px-3 py-2 t-label-xs text-ink-subtle">Runs</th></tr></thead><tbody>{rows.map((row,index)=><tr key={`${row.agent}-${row.model}`} className="leaderboard-row border-b border-edge-subtle last:border-b-0 hover:bg-hover" style={{"--chart-delay":`${index*75}ms`} as CSSProperties}><td className="px-4 py-3 t-mono-sm text-ink-subtle">{index+1}</td><td className="px-3 py-3"><p className="t-mono-md font-medium text-ink">{row.model}</p></td><td className="px-3 py-3 t-mono-sm text-ink-muted">{row.agent}</td><td className="px-3 py-3"><div className="flex items-center justify-between gap-2"><span className="t-mono-sm text-ink">{row.resolvedRate==null?"—":<CountUp to={row.resolvedRate} duration={720+index*80} format={(value)=>`${value.toFixed(1)}%`}/>}</span><span className="t-mono-xs text-ink-subtle">{row.resolved}/{row.evaluated}</span></div><div className="mt-1.5 h-1.5 overflow-hidden bg-inset"><div className="overview-chart-bar h-full origin-left bg-brand" style={{width:`${row.resolvedRate??0}%`,"--chart-delay":`${index*75}ms`} as CSSProperties}/></div></td><td className="px-3 py-3 t-mono-sm text-ink-muted">{row.averageCost==null?"—":formatCost(row.averageCost)}</td><td className="px-3 py-3 t-mono-sm text-ink-muted">{row.averageRuntime==null?"—":formatDuration(row.averageRuntime)}</td><td className="px-3 py-3 t-mono-sm text-ink">{row.runs}</td></tr>)}</tbody></table></div>{demo&&<p className="border-t border-edge bg-warn-soft px-4 py-2 t-mono-xs text-warn">Preview only—these values are not measured results. They disappear automatically when agent-attributed run exports are available.</p>}</section>;
}

function BenchmarkBars({rows,demo}:{rows:BenchmarkResult[];demo:boolean}){
  const visible=rows.filter((row)=>row.resolvedRate!=null).slice(0,8);return <section className="mt-4 border border-edge bg-surface"><header className="flex items-center justify-between border-b border-edge-subtle px-4 py-3"><div><h3 className="t-label text-ink">Resolved by agent + model</h3><p className="mt-1 t-body-sm text-ink-muted">One simple scale, sorted high to low.</p></div>{demo&&<span className="t-label-xs text-warn">demo</span>}</header><div className="p-4"><div className="ml-[min(42%,240px)] flex justify-between border-b border-edge-subtle pb-1 t-mono-xs text-ink-subtle"><span>0%</span><span>50%</span><span>100%</span></div><ol className="mt-3 space-y-3">{visible.map((row,index)=><li key={`${row.agent}-${row.model}`} className="grid grid-cols-[minmax(110px,42%)_1fr] items-center gap-3"><div className="min-w-0 text-right"><p className="truncate t-mono-xs text-ink" title={row.model}>{row.model}</p><p className="truncate t-mono-xs text-ink-subtle" title={row.agent}>{row.agent}</p></div><div className="flex items-center gap-2"><div className="h-5 flex-1 overflow-hidden bg-inset"><div className="overview-chart-bar h-full origin-left bg-brand/80" style={{width:`${row.resolvedRate}%`,"--chart-delay":`${index*80}ms`} as CSSProperties}/></div><span className="w-12 text-right t-mono-xs text-ink">{row.resolvedRate?.toFixed(1)}%</span></div></li>)}</ol></div></section>;
}

function Band({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`border-t border-edge-strong py-[var(--band-gap)] first:border-t-0 ${className}`}>{children}</section>;
}

export function OverviewPage() {
  const [scope, setScope] = useState<RunCategory | "all">("all");
  const { dashboard, loading, error } = useDashboard();
  if (loading) return <PageLoading />;
  if (error) return <PageError message={error} />;
  if (!dashboard) return <EmptyState variant="terminal" />;

  const runs = dashboard.runs;
  const metrics = calculateOverviewMetrics(runs);
  const review = runs.filter(requiresReview);
  const repositories = new Map(dashboard.cases.map((item) => [item.case_id, item.repository]));
  const benchmarkRuns=runs.filter((run)=>!(["sanity","diagnostic","synthetic"] as RunCategory[]).includes(deriveRunCategory(run,repositories.get(run.case_id))));
  const leaderboard=benchmarkResults(benchmarkRuns);
  const scopedRuns = runs.filter((run) => scope === "all" || deriveRunCategory(run, repositories.get(run.case_id)) === scope);
  const recentRuns = [...scopedRuns].sort((left, right) => (right.created_at ?? "").localeCompare(left.created_at ?? "") || right.run_id.localeCompare(left.run_id)).slice(0, 8);
  const flow: PipelineStep[] = [
    { label: "Frozen case", href: "/cases", tip: "Browse frozen benchmark cases.", count: dashboard.cases.length },
    { label: "Agent harness", href: "/runs", tip: "Inspect harness and model configurations.", count: runs.length },
    { label: "Generated patch", href: "/runs?patchApplied=true", tip: "View runs with an applied patch.", count: metrics.patchesCaptured },
    { label: "Deterministic evaluation", href: "/runs?evaluation=evaluated", tip: "View deterministically evaluated runs.", count: metrics.evaluated },
    { label: "Structured evidence", href: "/workflow", tip: "Inspect execution evidence and artifacts.", count: runs.length },
  ];

  return (
    <div className="overview-page">
      <Band className="pb-8 pt-6">
        <div className="grid items-center gap-8 xl:grid-cols-[minmax(320px,.8fr)_minmax(560px,1.2fr)]">
          <div><p className="flex items-center gap-2 t-label text-ink-subtle before:h-px before:w-5 before:bg-brand">CERN network engineering</p><span className="mt-3 inline-flex h-5 items-center bg-invert px-2 t-label text-invert-foreground">Multi-agent benchmark</span><h1 className="mt-3 t-display">PatchBench</h1><div className="mt-2 max-w-prose space-y-2 text-ink-muted"><p className="t-body text-ink">Compare different coding agents on the same reproducible bug-fix tasks.</p><p className="t-body">Every agent receives the same frozen case and is judged by the same hidden tests, so patches, reliability, cost, and runtime stay comparable.</p></div><div className="mt-5 flex flex-wrap gap-2"><Link to="/workflow" className="inline-flex min-h-9 items-center gap-2 bg-invert px-3 t-label text-invert-foreground transition-transform duration-2 hover:-translate-y-0.5"><Bot className="h-3.5 w-3.5"/>Build a benchmark</Link><Link to="/compare" className="inline-flex min-h-9 items-center gap-2 border border-edge-strong px-3 t-label text-ink transition-[background-color,transform] duration-2 hover:-translate-y-0.5 hover:bg-hover"><GitCompareArrows className="h-3.5 w-3.5"/>Compare agents</Link></div></div>
          <BenchmarkDimensions/>
        </div>
      </Band>

      <Band>
        <h2 className="mb-4 t-label text-ink-subtle">Metrics</h2>
        <div className="grid gap-4 xl:grid-cols-2 2xl:grid-cols-[1fr_1.4fr_1.2fr]">
          <MetricCluster title="Volume" columns={2}>
            <MetricCard standalone={false} label="Validated cases" value={<CountUp to={dashboard.cases.filter((item)=>item.validation_status).length} format={(value)=>formatNumber(Math.round(value))}/>} accessibleValue={formatNumber(dashboard.cases.filter((item)=>item.validation_status).length)} icon={Boxes} href="/cases" tooltip="Browse validated benchmark cases." />
            <MetricCard standalone={false} label="Exported runs" value={<CountUp to={runs.length} format={(value)=>formatNumber(Math.round(value))}/>} accessibleValue={formatNumber(runs.length)} icon={Network} href="/runs" tooltip="Browse all exported runs." />
          </MetricCluster>
          <MetricCluster title="Outcome">
            <MetricCard standalone={false} label="Resolved rate" value={metrics.resolvedRate==null?"—":<CountUp to={metrics.resolvedRate} format={(value)=>`${value.toFixed(1)}%`}/>} accessibleValue={metrics.resolvedRate==null?"unknown":`${metrics.resolvedRate.toFixed(1)}%; ${metrics.resolved} of ${metrics.knownOutcomes} known outcomes`} icon={ListChecks} href="/runs?resolved=true" tooltip="Resolved runs divided by runs with a known outcome; skipped or unknown outcomes are excluded." />
            <MetricCard standalone={false} label="Patch apply rate" value={metrics.patchApplyRate==null?"—":<CountUp to={metrics.patchApplyRate} format={(value)=>`${value.toFixed(1)}%`}/>} accessibleValue={metrics.patchApplyRate==null?"unknown":`${metrics.patchApplyRate.toFixed(1)}%; ${metrics.patchesApplied} of ${metrics.patchesCaptured} captured patches`} icon={ScrollText} href="/runs?patchApplied=true" tooltip="Applied patches divided by captured patches; runs without a patch are excluded." />
            <MetricCard standalone={false} compactValue label="Evaluation status" value={<><CountUp to={metrics.evaluated} format={(value)=>String(Math.round(value))}/>/<CountUp to={metrics.skipped} format={(value)=>String(Math.round(value))}/>/<CountUp to={metrics.evaluationUnknown} format={(value)=>String(Math.round(value))}/></>} accessibleValue={`${metrics.evaluated} evaluated, ${metrics.skipped} skipped, ${metrics.evaluationUnknown} unknown`} icon={TerminalSquare} href="/runs?evaluation=all" tooltip="Evaluated / skipped / unknown. Missing legacy states remain unknown instead of being counted as evaluated." />
            <MetricCard standalone={false} label="Partial patches" value={<CountUp to={metrics.partialPatches} format={(value)=>formatNumber(Math.round(value))}/>} accessibleValue={formatNumber(metrics.partialPatches)} icon={FileWarning} href="/runs?partialPatch=true" tooltip="View runs with partial patches." />
          </MetricCluster>
          <MetricCluster title="Cost" className="xl:col-span-2 2xl:col-span-1">
            <MetricCard standalone={false} label="Average runtime" value={metrics.averageRuntime==null?"—":<CountUp to={metrics.averageRuntime} format={formatDuration}/>} accessibleValue={formatDuration(metrics.averageRuntime)} icon={Clock3} href="/runs?sort=runtime" tooltip={`Average across ${metrics.runtimeCoverage} of ${runs.length} runs with a reported runtime; missing values are excluded rather than treated as zero.`} />
            <MetricCard standalone={false} label="Input tokens" value={metrics.inputCoverage?<CountUp to={metrics.inputTokens} format={(value)=>formatCompactNumber(Math.round(value))}/>:"—"} accessibleValue={metrics.inputCoverage?`${formatNumber(metrics.inputTokens)} total; ${metrics.inputCoverage} of ${runs.length} runs reported`:"not reported"} icon={Activity} href="/runs?sort=inputTokens" tooltip={`Total reported input tokens across ${metrics.inputCoverage} of ${runs.length} runs. Exact values remain available in run details.`} />
            <MetricCard standalone={false} label="Output tokens" value={metrics.outputCoverage?<CountUp to={metrics.outputTokens} format={(value)=>formatCompactNumber(Math.round(value))}/>:"—"} accessibleValue={metrics.outputCoverage?`${formatNumber(metrics.outputTokens)} total; ${metrics.outputCoverage} of ${runs.length} runs reported`:"not reported"} icon={Gauge} href="/runs?sort=outputTokens" tooltip={`Total reported output tokens across ${metrics.outputCoverage} of ${runs.length} runs. Exact values remain available in run details.`} />
            <MetricCard standalone={false} label="Cost total" value={metrics.costCoverage?<CountUp to={metrics.reportedCost} format={formatCost}/>:"—"} accessibleValue={metrics.costCoverage?`${formatCost(metrics.reportedCost)} total; ${metrics.costCoverage} of ${runs.length} runs reported`:"not reported"} icon={Banknote} href="/runs?sort=cost" tooltip={`Sum of provider-reported costs across ${metrics.costCoverage} of ${runs.length} runs; missing costs are excluded.`} />
          </MetricCluster>
        </div>
      </Band>

      <Band>
        <BenchmarkLeaderboard rows={leaderboard.rows} demo={leaderboard.demo}/>
        <BenchmarkBars rows={leaderboard.rows} demo={leaderboard.demo}/>
      </Band>

      <Band>
        <h2 id="pipeline-heading" className="mb-3 t-label text-ink-subtle">Evaluation pipeline</h2>
        <PipelineBand steps={flow} />
      </Band>

      <Band>
        <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
          <h2 className="t-label text-ink-subtle">Recent runs</h2>
          <Tabs value={scope} onValueChange={(value) => setScope(value as RunCategory | "all")}>
            <TabsList aria-label="Recent run scope">
              {(["benchmark", "sanity", "diagnostic", "synthetic", "all"] as const).map((item) => <TabsTrigger key={item} value={item}>{runCategoryLabels[item]}</TabsTrigger>)}
            </TabsList>
          </Tabs>
        </div>
        <RunTable runs={recentRuns} pageSize={8} compact hideControls />
      </Band>

      <Band className="pb-0">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3"><div className={review.length?"border-l-2 border-l-brand pl-3":""}><h2 className={review.length?"t-label text-ink":"t-label text-ink-subtle"}>Needs review <span className={review.length?"ml-1 t-mono-sm text-brand":"ml-1 t-mono-sm"}>· {review.length} runs</span></h2></div><Link to="/runs" className="inline-flex min-h-11 items-center t-label text-ink-muted underline-offset-4 hover:text-ink hover:underline md:min-h-0">View all →</Link></div>
        {review.length?<RunTable runs={review} pageSize={5} compact hideControls/>:<EmptyState title="No runs currently require review." description="Newly exported runs that need attention will appear here."/>}
      </Band>
    </div>
  );
}
