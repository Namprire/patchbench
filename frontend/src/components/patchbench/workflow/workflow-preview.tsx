import { useState } from "react";
import { ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
import type { RunSummary } from "../../../types/dashboard";
import { StageStrip } from "./stage-strip";
import { applicableProgress } from "./workflow-layout";

export function WorkflowPreview({run}:{run:RunSummary}){const progress=applicableProgress(run.workflow);const[selected,setSelected]=useState<string|null>(null);return <section className="border border-edge bg-surface"><header className="flex min-h-8 items-center gap-3 border-b border-edge-subtle px-4 py-1"><span className="t-label text-ink-subtle">Execution trace</span><span className="t-mono-xs text-ink-muted">{progress.completed} of {progress.total} stages recorded</span><Link aria-label="Open execution trace for this run" className="ml-auto inline-flex min-h-11 items-center gap-1 t-label-xs text-brand-text hover:underline md:min-h-0" to={`/runs/${encodeURIComponent(run.case_id)}/${encodeURIComponent(run.run_id)}?tab=workflow`}>Open trace<ArrowRight className="h-3.5 w-3.5"/></Link></header><div className="p-3"><StageStrip workflow={run.workflow} selectedStageId={selected} onSelect={setSelected}/></div></section>}
