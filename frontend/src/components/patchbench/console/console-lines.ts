import type { Workflow, WorkflowDiagnostic } from "../../../types/dashboard";
import { formatDuration } from "../../../lib/utils";

export type ConsoleSeverity = "stage" | "err" | "warn" | "ok" | "info";
export type ConsoleSource = "workflow" | "diagnostic" | "trajectory" | "events" | "mcp" | "debug" | "test";

export interface ConsoleLine {
  seq: number;
  t: string | null;
  sev: ConsoleSeverity;
  source: ConsoleSource;
  stageId: string | null;
  message: string;
  raw: string | unknown;
  diagnosticCode?: string;
  parseError?: boolean;
}

const timestampPattern = /^(?:(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)|\[?(\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]?)(?:\s+|\s*[-|]\s*)/;

function parseTimestamp(line: string): { t: string | null; message: string } {
  const match = line.match(timestampPattern);
  if (!match) return { t: null, message: line };
  return { t: match[1] ?? match[2] ?? null, message: line.slice(match[0].length) };
}

function severityFromText(text: string): ConsoleSeverity {
  if (/(?:\bERROR\b|\bERR\b|\bFATAL\b|\bCRITICAL\b|Traceback|\[error\])/i.test(text)) return "err";
  if (/(?:\bWARN\b|\bWARNING\b|\[warn\])/i.test(text)) return "warn";
  if (/(?:\bPASSED\b|\bOK\b|\bSUCCESS\b|\bapplied\b)/i.test(text)) return "ok";
  return "info";
}

function severityFromObject(value: Record<string, unknown>, fallback: string): ConsoleSeverity {
  const explicit = typeof value.level === "string" ? value.level : typeof value.severity === "string" ? value.severity : null;
  return explicit ? severityFromText(explicit) : severityFromText(fallback);
}

function objectTimestamp(value: Record<string, unknown>): string | null {
  for (const key of ["timestamp", "time", "created_at", "at"]) {
    const candidate = value[key];
    if (typeof candidate === "string") {
      const parsed = parseTimestamp(`${candidate} `);
      if (parsed.t) return parsed.t;
    }
  }
  return null;
}

function objectSummary(value: Record<string, unknown>): string {
  for (const key of ["message", "msg", "event", "type", "name", "tool", "method"]) {
    const candidate = value[key];
    if (typeof candidate === "string" && candidate.trim()) return `${key}=${candidate}`;
  }
  return JSON.stringify(value).replaceAll(/\s+/g, " ").slice(0, 280);
}

export function deriveWorkflowLines(workflow: Workflow): ConsoleLine[] {
  const lines: ConsoleLine[] = [];
  for (const node of workflow.nodes) {
    if (node.started_at) lines.push({ seq: lines.length, t: node.started_at, sev: "stage", source: "workflow", stageId: node.id, message: `▶ ${node.label}`, raw: node });
    if (node.completed_at) lines.push({ seq: lines.length, t: node.completed_at, sev: "stage", source: "workflow", stageId: node.id, message: `■ ${node.label} · ${node.status.toUpperCase()} · ${formatDuration(node.duration_seconds)}`, raw: node });
  }
  return lines;
}

export function deriveDiagnosticLines(diagnostics: WorkflowDiagnostic[]): ConsoleLine[] {
  return diagnostics.map((item, seq) => ({
    seq,
    t: null,
    sev: item.severity === "error" ? "err" : item.severity === "warning" ? "warn" : "info",
    source: "diagnostic",
    stageId: item.node_id,
    message: `${item.code} · ${item.message.slice(0, 80)}`,
    raw: item,
    diagnosticCode: item.code,
  }));
}

type ArtifactSource = Exclude<ConsoleSource, "workflow" | "diagnostic">;

function fromObject(source: ArtifactSource, value: Record<string, unknown>, seq: number): ConsoleLine {
  const message = objectSummary(value);
  const stage = typeof value.stage_id === "string" ? value.stage_id : typeof value.stageId === "string" ? value.stageId : null;
  return { seq, t: objectTimestamp(value), sev: severityFromObject(value, message), source, stageId: stage, message, raw: value };
}

export function deriveArtifactLines(source: ArtifactSource, content: unknown): ConsoleLine[] {
  if (content == null) return [];
  if (Array.isArray(content)) return content.map((value, seq) => typeof value === "object" && value !== null ? fromObject(source, value as Record<string, unknown>, seq) : (()=>{const parsed=parseTimestamp(String(value));return {seq,t:parsed.t,sev:severityFromText(parsed.message),source,stageId:null,message:parsed.message,raw:value}})());
  if (typeof content === "object") return [fromObject(source, content as Record<string, unknown>, 0)];
  const text = String(content);
  if ((source === "events" || source === "mcp") && (text.trim().startsWith("[") || text.trim().startsWith("{"))) {
    try {
      const parsed: unknown = JSON.parse(text);
      const records = Array.isArray(parsed) ? parsed : [parsed];
      return records.map((value, seq) => typeof value === "object" && value !== null ? fromObject(source, value as Record<string, unknown>, seq) : { seq, t:null, sev:"info", source, stageId:null, message:String(value), raw:value });
    } catch {
      return [{ seq:0, t:null, sev:"warn", source, stageId:null, message:`${source.toUpperCase()} ACTIVITY — COULD NOT BE PARSED`, raw:content, parseError:true }];
    }
  }
  return text.split(/\r?\n/).map((raw, seq) => { const parsed=parseTimestamp(raw); return { seq, t:parsed.t, sev:severityFromText(parsed.message), source, stageId:null, message:parsed.message, raw }; });
}

function comparableTime(value: string | null): string | null {
  if (!value) return null;
  const clock = value.includes("T") ? value.split("T")[1]?.replace(/Z|[+-]\d{2}:?\d{2}$/, "") : value;
  return clock ?? null;
}

export function mergeConsoleLines(workflowLines: ConsoleLine[], diagnosticLines: ConsoleLine[], ...artifactGroups: ConsoleLine[][]): ConsoleLine[] {
  const boundaries = [...workflowLines, ...diagnosticLines].map((line, index) => ({ line, index })).sort((a,b) => {
    const aTime=comparableTime(a.line.t);const bTime=comparableTime(b.line.t);
    if (aTime && bTime) return aTime.localeCompare(bTime) || a.index-b.index;
    if (aTime) return -1;
    if (bTime) return 1;
    return a.index-b.index;
  }).map(({line})=>line);
  return [...boundaries, ...artifactGroups.flat()].map((line,seq)=>({...line,seq}));
}

export function filterConsoleLines(lines: ConsoleLine[], filters: { severities: Set<ConsoleSeverity>; sources: Set<ConsoleSource>; search?: string; stageId?: string | null }): ConsoleLine[] {
  const term=filters.search?.trim().toLowerCase()??"";
  return lines.filter((line)=>filters.severities.has(line.sev)&&filters.sources.has(line.source)&&(!filters.stageId||line.stageId===filters.stageId)&&(!term||line.message.toLowerCase().includes(term)));
}
