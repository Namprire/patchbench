import { describe, expect, it } from "vitest";
import { deriveArtifactLines, deriveDiagnosticLines, deriveWorkflowLines, filterConsoleLines, mergeConsoleLines } from "../components/patchbench/console/console-lines";
import { maxTurnRun } from "./fixtures";

describe("execution console derivation", () => {
  it("is deterministic and preserves workflow, diagnostic, and artifact evidence", () => {
    const workflow={...maxTurnRun.workflow,nodes:maxTurnRun.workflow.nodes.map((node,index)=>({...node,started_at:`2026-01-01T00:00:${String(index*2).padStart(2,"0")}Z`,completed_at:index===1?null:`2026-01-01T00:00:${String(index*2+1).padStart(2,"0")}Z`}))};
    const derive=()=>mergeConsoleLines(deriveWorkflowLines(workflow),deriveDiagnosticLines(workflow.diagnostics),deriveArtifactLines("trajectory","12:00:00 reading file\nplain line"),deriveArtifactLines("events",[{level:"warning",type:"tool",stage_id:"agent"}]));
    expect(derive()).toEqual(derive());
    expect(derive().filter((line)=>line.source==="workflow"&&line.sev==="stage").length).toBeGreaterThan(0);
    expect(derive().filter((line)=>line.source==="diagnostic")).toHaveLength(workflow.diagnostics.length);
    expect(derive().filter((line)=>line.source==="trajectory")).toHaveLength(2);
  });

  it("never guesses a missing timestamp", () => {
    const [line]=deriveArtifactLines("trajectory","Reading configuration…");
    expect(line.t).toBeNull();
    expect(line.message).toBe("Reading configuration…");
  });

  it("filters by severity, source, stage, and free text without mutating the source", () => {
    const lines=mergeConsoleLines([],deriveDiagnosticLines(maxTurnRun.workflow.diagnostics),deriveArtifactLines("trajectory","WARN agent stalled\nOK patch applied"));
    const snapshot=structuredClone(lines);
    const result=filterConsoleLines(lines,{severities:new Set(["warn"]),sources:new Set(["diagnostic"]),stageId:"termination",search:"turn"});
    expect(result).toHaveLength(1);
    expect(result[0].diagnosticCode).toBe("AGENT_MAX_TURNS");
    expect(lines).toEqual(snapshot);
  });
});
