export const DASHBOARD_SCHEMA_VERSION = 1;

export interface Manifest { schema_version: number; generated_at: string; case_count: number; run_count: number; includes_gold: boolean }
export interface PatchComparison { agent_files_changed: string[]; gold_files_changed: string[]; overlapping_files: string[]; file_overlap_count: number; file_overlap_jaccard: number; exact_file_set_match: boolean; agent_lines_added: number; agent_lines_removed: number; gold_lines_added: number; gold_lines_removed: number }
export type CaseType = "coding" | "landb_answer";
export type CaseOperation = "validate-case" | "run" | "evaluate-patch" | "run-landb-answer" | "verify-landb-dev";
export interface CaseSummary { schema_version: number; case_id: string; case_type: CaseType; allowed_operations: CaseOperation[]; repository: string | null; issue_title: string; base_commit: string | null; fix_commit: string | null; validation_status: boolean | null; draft: boolean; evaluation: { test_command: string | null; setup_command_count: number; workdir: string | null; timeout_seconds: number | null }; gold_files_changed: string[]; run_count: number; last_run_at: string | null }
export interface CaseDetail extends CaseSummary { issue_text: string; safe_provenance: unknown; human_reference_patch: string | null; run_ids: string[] }
export type WorkflowHealth = "running" | "healthy" | "degraded" | "stalled" | "error" | "unknown";
export type WorkflowHealthSource = "authoritative" | "derived" | "historical_fallback";
export type WorkflowTaskOutcome = "resolved" | "unresolved" | "not_evaluated" | "diagnostic" | "unknown";
export type WorkflowNodeStatus = "initial" | "queued" | "running" | "success" | "warning" | "error" | "skipped" | "unavailable";
export type WorkflowEdgeStatus = "inactive" | "active" | "completed" | "warning" | "error" | "skipped";
export interface WorkflowDiagnostic { severity: "info" | "warning" | "error"; code: string; message: string; node_id: string; evidence: { field: string; value: unknown }; inspection_target: string }
export interface WorkflowNodeData { id: string; kind: string; label: string; status: WorkflowNodeStatus; status_reason: string | null; started_at: string | null; completed_at: string | null; duration_seconds: number | null; summary: string; metrics: Record<string, unknown>; artifact_refs: string[]; diagnostic_codes: string[] }
export interface WorkflowEdgeData { id: string; source: string; target: string; status: WorkflowEdgeStatus; label: string | null; animated: boolean }
export interface Workflow { schema_version: number; health: WorkflowHealth; health_source: WorkflowHealthSource; task_outcome: WorkflowTaskOutcome; started_at: string | null; last_activity_at: string | null; last_activity_age_seconds: number | null; completed_at: string | null; freshness_seconds: number; nodes: WorkflowNodeData[]; edges: WorkflowEdgeData[]; diagnostics: WorkflowDiagnostic[] }
export interface RunSummary {
  schema_version: number; case_id: string; case_type: CaseType; run_id: string; case_title: string | null; created_at: string | null; historical: boolean;
  agent: string | null; agent_harness: string | null; agent_harness_version: string | null; requested_model: string | null; actual_model: string | null; model_source: string | null; provider: string | null; provider_host: string | null;
  prompt_mode: string | null; prompt_hash: string | null; available_relevant_files_count: number | null; included_relevant_files_count: number | null;
  configured_max_turns: number | null; reported_turns: number | null; timeout_seconds: number | null; runtime_seconds: number | null;
  activity_observed: boolean | null; event_count: number | null; tool_event_count: number | null; first_event_seconds: number | null; last_event_seconds: number | null;
  termination_reason: string | null; execution_detail: string | null; stop_reason: string | null;
  mcp_enabled: boolean; mcp_servers: string[]; mcp_condition: string; mcp_server_name: string | null; mcp_profile: string | null; mcp_revision: string | null; mcp_revision_dirty: boolean | null;
  mcp_process_started: boolean | null; mcp_tools_discovered: boolean | null; mcp_activity_observed: boolean; mcp_tool_call_count: number; mcp_tool_success_count: number; mcp_tool_error_count: number; mcp_telemetry_available: boolean;
  patch_captured: boolean; partial_patch_captured: boolean; patch_applied: boolean | null; evaluation_skipped: boolean | null; evaluation_skip_reason: string | null; resolved: boolean | null; failure_mode: string | null;
  files_changed: string[]; lines_added: number | null; lines_removed: number | null; patch_comparison: PatchComparison | null;
  input_tokens: number | null; output_tokens: number | null; cache_read_tokens: number | null; reported_cost_usd: number | null;
  workflow: Workflow;
  run_config?: Record<string, unknown> | null;
}
export interface RunDetail extends RunSummary { artifacts: { prompt: string | null; answer: string | null; agent_patch: string | null; trajectory: string | null; test_log: string | null; agent_events: unknown[] | null; agent_debug: string | null; mcp_activity: string | null; human_reference_patch: string | null }; raw_sanitized_metadata: Record<string, unknown> }
export interface DashboardData extends Manifest { cases: CaseSummary[]; runs: RunSummary[] }
