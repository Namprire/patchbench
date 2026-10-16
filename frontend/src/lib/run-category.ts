import type { RunSummary } from "../types/dashboard";

export type RunCategory = "benchmark" | "sanity" | "diagnostic" | "synthetic" | "unknown";

const diagnosticTerms = [
  "audit",
  "check",
  "dev-",
  "metric",
  "observability",
  "report-",
  "smoke",
  "timeout",
];

export function deriveRunCategory(run: RunSummary, repository?: string | null): RunCategory {
  const caseId = run.case_id.toLowerCase();
  const runId = run.run_id.toLowerCase();
  const repo = repository?.toLowerCase() ?? "";

  if (caseId.startsWith("toy-") || caseId.startsWith("synthetic-") || repo === "toy_repo" || repo === "fixture") return "synthetic";
  if (diagnosticTerms.some((term) => runId.includes(term))) return "diagnostic";
  if (runId.includes("sanity") || run.agent === "gold" || run.agent === "dummy") return "sanity";
  if (run.agent === "claude-code" || run.agent_harness === "claude-code-cli" || (run.historical && caseId.startsWith("cfmgr-"))) return "benchmark";
  return "unknown";
}

export function runContextLabel(run: RunSummary, repository?: string | null): string | null {
  if (run.historical) return run.termination_reason ? "Historical schema" : "Historical schema · Termination metadata unavailable";
  const category = deriveRunCategory(run, repository);
  const context = run.agent === "gold" ? "Gold sanity run" : run.agent === "dummy" ? "Dummy baseline" : category === "diagnostic" ? "Diagnostic run" : category === "synthetic" ? "Synthetic fixture" : null;
  const status = !run.termination_reason ? "Termination metadata unavailable" : run.evaluation_skipped && run.termination_reason === "max_turns" ? "Evaluation skipped after max turns" : !run.patch_captured ? "Agent patch not recorded" : null;
  return [context,status].filter(Boolean).join(" · ")||null;
}

export const runCategoryLabels: Record<RunCategory | "all", string> = {
  benchmark: "Benchmark",
  sanity: "Sanity",
  diagnostic: "Diagnostic",
  synthetic: "Synthetic",
  unknown: "Unknown",
  all: "All",
};
