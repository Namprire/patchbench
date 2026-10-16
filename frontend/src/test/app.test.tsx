import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ComparisonProvider, DashboardProvider } from "../data/dashboard";
import { buildCommand, isValidRunId } from "../pages/command-builder-page";
import { emptyRunFilters, filterRuns } from "../components/patchbench/run-filters";
import { deriveRunCategory } from "../lib/run-category";
import { configFromRun, defaultWorkflowConfig, toRunRequest, workflowConfigError, WORKFLOW_CONFIG_STORAGE_KEY } from "../lib/workflow-config";
import { benchmarkResults, calculateOverviewMetrics } from "../pages/overview-page";
import { caseDetail, dashboard, historicalRun, manifest, maxTurnRun } from "./fixtures";

function mockData(version = 1) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    let payload: unknown;
    if (path.includes("manifest")) payload = { ...manifest, schema_version: version };
    else if (path.includes("dashboard")) payload = { ...dashboard, schema_version: version };
    else if (path.includes("fixture-partial")) payload = maxTurnRun;
    else if (path.includes("historical")) payload = historicalRun;
    else if (path.includes("cases/synthetic-001")) payload = caseDetail;
    else return new Response("missing", { status: 404 });
    return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}
function renderApp(path = "/") { return render(<MemoryRouter initialEntries={[path]}><DashboardProvider><ComparisonProvider><App /></ComparisonProvider></DashboardProvider></MemoryRouter>); }

describe("PatchBench evaluation workspace", () => {
  beforeEach(() => {localStorage.clear();mockData()});

  it("labels synthetic example outcomes separately from research results", async () => {
    const originalFetch = globalThis.fetch;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (!String(input).includes("dashboard")) return originalFetch(input);
      return new Response(JSON.stringify({ ...dashboard, cases: dashboard.cases.map((item) =>
        ({ ...item, issue_title: "Synthetic demo: word labels" })) }), { status: 200 });
    }));
    renderApp();
    expect(await screen.findByText(/Synthetic examples: these outcomes/)).toBeInTheDocument();
  });

  it("renders the dashboard and max-turn partial-patch evidence", async () => {
    renderApp();
    expect(await screen.findByRole("heading", { name: "PatchBench" })).toBeInTheDocument();
    expect(screen.getByText("Compare different coding agents on the same reproducible bug-fix tasks.")).toBeInTheDocument();
    expect(screen.getByLabelText("Benchmark dimensions")).toBeInTheDocument();
    expect(screen.getByLabelText("Benchmark results")).toBeInTheDocument();
    expect(screen.getByText("Ranked by resolved rate for each distinct agent and model pair.")).toBeInTheDocument();
    expect(screen.getByText("Illustrative demo")).toBeInTheDocument();
    expect(document.querySelectorAll("[data-count-target]").length).toBeGreaterThan(0);
    expect(screen.queryByText(/Observe.*Understand/i)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Build" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Tools" })).not.toBeInTheDocument();
    expect(screen.getAllByText("max turns").length).toBeGreaterThan(0);
    expect(screen.getByText("Partial patches")).toBeInTheDocument();
    expect(screen.getByText("Evaluation status")).toBeInTheDocument();
    expect(screen.queryByText(/known outcomes/)).not.toBeInTheDocument();
    expect(screen.queryByText(/reported$/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Resolved rate/ })).toHaveAttribute("href", "/runs?resolved=true");
    expect(screen.getByRole("link", { name: /Average runtime/ })).toHaveAttribute("href", "/runs?sort=runtime");
    expect(screen.getByText("Evaluation pipeline")).toBeInTheDocument();
  });

  it("groups arbitrary agent and model pairs for benchmark results", () => {
    const result=benchmarkResults(dashboard.runs);
    expect(result.demo).toBe(false);
    expect(result.rows).toContainEqual(expect.objectContaining({agent:"claude-code",model:"fixture-model",runs:1}));
    expect(result.rows).toContainEqual(expect.objectContaining({agent:"claude-code",model:"unknown model",runs:1}));
  });

  it("does not rank unknown evaluation states as evaluated", () => {
    const result=benchmarkResults([{...dashboard.runs[0],evaluation_skipped:null,resolved:true}]);
    expect(result.rows[0]).toMatchObject({runs:1,evaluated:0,resolved:0,resolvedRate:null});
  });

  it("calculates overview metrics only from applicable, reported values", () => {
    const metrics = calculateOverviewMetrics(dashboard.runs);
    expect(metrics).toMatchObject({
      evaluated: 1,
      skipped: 1,
      evaluationUnknown: 0,
      resolved: 1,
      knownOutcomes: 2,
      resolvedRate: 50,
      patchesCaptured: 1,
      patchesApplied: 0,
      patchApplyRate: 0,
      partialPatches: 1,
      averageRuntime: 12,
      runtimeCoverage: 2,
      inputTokens: 200,
      inputCoverage: 2,
      outputTokens: 20,
      outputCoverage: 2,
      reportedCost: 0,
      costCoverage: 0,
    });
  });

  it("restores persisted execution settings when reusing a run", () => {
    const config=configFromRun({case_id:"case-a",run_id:"run-a",agent:"fallback-agent",requested_model:null,prompt_mode:null,configured_max_turns:null,timeout_seconds:null,mcp_server_name:null,mcp_profile:null,run_config:{agent:"custom-agent",requested_model:"model-x",prompt_mode:"issue_with_hints",agent_max_turns:33,agent_timeout_seconds:77,container:true,agent_debug:true,agent_image:"solver:v2",agent_network:"bridge",mcp_server:"landb",mcp_profile:"broad"}});
    expect(config).toMatchObject({agent:"custom-agent",model:"model-x",promptMode:"issue_with_hints",maxTurns:33,timeout:77,container:true,debug:true,image:"solver:v2",network:"bridge",mcpServer:"landb",mcpProfile:"broad"});
  });

  it("builds a structured LanDB answer request and CLI equivalent", () => {
    const config={...defaultWorkflowConfig("answer-a"),caseType:"landb_answer" as const,answerCondition:"broad" as const,runId:"answer-r1",model:"model-x",maxTurns:8,timeout:300,landbMcpRepo:"../landb-mcp"};
    const request=toRunRequest(config,"evaluation");
    const command=buildCommand(config);
    expect(request).toMatchObject({case_id:"answer-a",agent:"claude-code",answer_condition:"broad",container:false,live_dev:false});
    expect(command).toContain("patchbench run-landb-answer");
    expect(command).toContain("run-landb-answer 'answer-a'");
    expect(command).toContain("--condition 'broad'");
    expect(command).not.toContain("--prompt-mode");
  });

  it("maps a custom container command into the run request", () => {
    const config={...defaultWorkflowConfig("case-a"),agent:"container-command",container:true,agentCommand:"solve --prompt /run/prompt"};
    expect(toRunRequest(config,"evaluation")).toMatchObject({agent:"container-command",container:true,agent_command:"solve --prompt /run/prompt"});
    expect(buildCommand(config)).toContain("--agent-command 'solve --prompt /run/prompt'");
    expect(buildCommand(config)).toContain("patchbench run 'case-a'");
  });

  it("requires credentials before starting containerized Claude Code", () => {
    const config={...defaultWorkflowConfig("case-a"),container:true,envFile:"",model:"fixture-model"};
    expect(workflowConfigError(config)).toMatch(/requires an Agent env file/);
    expect(workflowConfigError({...config,envFile:"~/.config/patchbench/claude-env.sh"})).toBeNull();
  });

  it("requires an explicit model for real Claude runs but not prompt preparation", () => {
    const config=defaultWorkflowConfig("case-a");
    expect(workflowConfigError(config)).toMatch(/Select or enter a model/);
    expect(workflowConfigError(config,"prompt")).toBeNull();
    expect(workflowConfigError({...config,agent:"dummy"})).toBeNull();
    expect(workflowConfigError({...config,model:"fixture-model"})).toBeNull();
  });

  it("uses one two-state sun and moon theme control", async () => {
    renderApp();
    const toggle=await screen.findByRole("button",{name:"Switch to dark mode"});
    expect(screen.queryByRole("radio",{name:"System"})).not.toBeInTheDocument();
    await userEvent.click(toggle);
    expect(document.documentElement).toHaveClass("dark");
    expect(screen.getByRole("button",{name:"Switch to light mode"})).toBeInTheDocument();
    expect(localStorage.getItem("patchbench.theme")).toBe("dark");
  });

  it("shows all recent evidence by default and allows narrower scopes", async () => {
    renderApp();
    expect(await screen.findByRole("tab", { name: "All" })).toHaveAttribute("data-state", "active");
    expect((await screen.findAllByRole("row", { name: "Open run fixture-partial" })).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("tab", { name: "Synthetic" }));
    expect((await screen.findAllByRole("row", { name: "Open run fixture-partial" })).length).toBeGreaterThan(0);
  });

  it("derives run categories conservatively", () => {
    expect(deriveRunCategory(dashboard.runs[0], "fixture")).toBe("synthetic");
    expect(deriveRunCategory({ ...dashboard.runs[0], case_id:"demo-1000", run_id:"unclassified", agent:null, agent_harness:null })).toBe("unknown");
  });

  it("shows a clear empty state", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(String(input).includes("manifest") ? { ...manifest, run_count: 0 } : { ...dashboard, runs: [], run_count: 0 }), { status: 200 })));
    renderApp("/runs");
    expect(await screen.findByText("No runs match these filters.")).toBeInTheDocument();
  });

  it("rejects an unknown future schema", async () => {
    mockData(99); renderApp();
    expect(await screen.findByText("The exported run data could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/Unsupported dashboard schema 99/)).toBeInTheDocument();
  });

  it("rejects malformed current-schema payloads before rendering routes", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const payload = String(input).includes("manifest") ? { schema_version: 1 } : dashboard;
      return new Response(JSON.stringify(payload), { status: 200 });
    }));
    renderApp();
    expect(await screen.findByText("The exported run data could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/Invalid dashboard manifest/)).toBeInTheDocument();
  });

  it("renders historical missing fields as unknown", async () => {
    renderApp("/runs/synthetic-001/historical");
    expect(await screen.findByText(/This run uses an earlier result schema/)).toBeInTheDocument();
    expect(screen.getAllByText("unknown").length).toBeGreaterThan(0);
  });

  it("keeps the reference patch hidden when gold is absent and escapes logs", async () => {
    renderApp("/runs/synthetic-001/historical");
    await screen.findByText(/This run uses an earlier result schema/);
    await userEvent.click(screen.getByRole("tab", { name: "Artifacts" }));
    expect(screen.queryByRole("tab", { name: "Reference patch" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Test log" }));
    expect(screen.getByText("<script>not executable</script>")).toBeInTheDocument();
    expect(document.querySelector("script:not([type])")).toBeNull();
  });

  it("filters runs by model and partial state", () => {
    expect(filterRuns(dashboard.runs, { ...emptyRunFilters, model: "fixture-model", partial: "yes" })).toHaveLength(1);
    expect(filterRuns(dashboard.runs, { ...emptyRunFilters, search: "historical" })[0].run_id).toBe("historical");
  });

  it("keeps browse rows clickable and keyboard accessible", async () => {
    renderApp("/runs");
    await screen.findByRole("heading", { name: "Runs" });
    expect(screen.queryByLabelText("Compare fixture-partial")).not.toBeInTheDocument();
    const row=screen.getByRole("row", { name: "Open run fixture-partial" });
    row.focus();
    fireEvent.keyDown(row,{key:"Enter"});
    expect(await screen.findByRole("heading",{name:"Run details"})).toBeInTheDocument();
    expect(await screen.findByTestId("workflow-canvas")).toBeInTheDocument();
  });

  it("selects runs in compare mode and opens a side-by-side comparison", async () => {
    renderApp("/runs");
    await screen.findByRole("heading", { name: "Runs" });
    await userEvent.click(screen.getByRole("radio", { name: "Compare" }));
    await userEvent.click(screen.getByLabelText("Compare fixture-partial"));
    await userEvent.click(screen.getByLabelText("Compare historical"));
    const tray=screen.getByLabelText("Run comparison selection");
    expect(within(tray).getByText("fixture-partial")).toBeInTheDocument();
    expect(within(tray).getByText("historical")).toBeInTheDocument();
    await userEvent.click(within(tray).getByRole("link", { name: "Compare" }));
    await waitFor(() => expect(screen.getByText("2 runs aligned")).toBeInTheDocument());
    expect(screen.getByText("Hints available / included")).toBeInTheDocument();
  });

  it("shows URL-derived filters as removable chips", async () => {
    renderApp("/runs?partialPatch=true");
    await screen.findByRole("heading", { name: "Runs" });
    expect(screen.getByRole("button",{name:"Remove Partial patch filter"})).toHaveTextContent("Partial patch: yes");
    expect(screen.getAllByText("1 results").length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button",{name:"Remove Partial patch filter"}));
    expect(screen.queryByRole("button",{name:"Remove Partial patch filter"})).not.toBeInTheDocument();
  });

  it("copies a run ID and confirms the action", async () => {
    renderApp("/runs");
    await screen.findByRole("heading", { name: "Runs" });
    await userEvent.click(screen.getByLabelText("Actions for fixture-partial"));
    await userEvent.click(await screen.findByRole("menuitem",{name:/Copy run ID/}));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("fixture-partial");
    expect(await screen.findByText("Run ID copied")).toBeInTheDocument();
  });

  it("marks the active navigation route beyond text color", async () => {
    renderApp("/runs");
    await screen.findByRole("heading", { name: "Runs" });
    expect(screen.getByRole("link",{name:"Runs"})).toHaveAttribute("aria-current","page");
  });

  it("explains unknown historical status metadata", async () => {
    renderApp("/runs");
    await screen.findByRole("heading", { name: "Runs" });
    const row=screen.getByRole("row",{name:"Open run historical"});
    const unknown=within(row).getAllByText("unknown").find((item)=>item.closest('[data-state="closed"]'));
    expect(unknown).toBeDefined();
    await userEvent.hover(unknown!);
    expect((await screen.findAllByText(/metadata is unavailable/i)).length).toBeGreaterThan(0);
  });

  it("shows loading skeletons and keeps the previous export on refresh failure", async () => {
    vi.stubGlobal("fetch",vi.fn(()=>new Promise<Response>(()=>undefined)));
    const loadingRender=renderApp("/runs");
    expect(document.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
    loadingRender.unmount();
    mockData();
    renderApp("/runs");
    await screen.findByRole("heading", { name: "Runs" });
    vi.stubGlobal("fetch",vi.fn(async()=>{throw new Error("export unavailable") }));
    await userEvent.click(screen.getByLabelText("Refresh exported data"));
    expect(await screen.findByText(/previous export remains available/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Runs" })).toBeInTheDocument();
  });

  it("generates a quoted command and warns for duplicate run IDs", async () => {
    const command = buildCommand({ caseId:"synthetic-001",agent:"claude-code",runId:"new run",model:"fixture-model",promptMode:"issue_only",maxTurns:20,timeout:60,container:true,live:true,debug:true });
    expect(command).toContain("--agent-model 'fixture-model'");
    expect(command).toContain("--run-id 'new run'");
    expect(isValidRunId("../bad")).toBe(false);
    renderApp("/command-builder");
    await screen.findByRole("heading", { name: "Run command" });
    expect(screen.getByRole("button", { name: "Copy command" })).toBeInTheDocument();
    const input = screen.getByLabelText("Run ID");
    await userEvent.clear(input); await userEvent.type(input, "fixture-partial");
    expect(await screen.findByText("Run ID already exists")).toBeInTheDocument();
  });

  it("does not put prohibited evaluation artifacts in synthetic fixtures", () => {
    expect(JSON.stringify({manifest,dashboard,maxTurnRun,historicalRun,caseDetail})).not.toContain("test.patch");
  });

  it("renders a stored run trace inside its run detail", async () => {
    renderApp("/runs/synthetic-001/fixture-partial?tab=workflow");
    expect(await screen.findByRole("heading", { name: "Run details" })).toBeInTheDocument();
    const canvas = await screen.findByTestId("workflow-canvas");
    expect(canvas).toHaveAttribute("data-nodes-draggable", "false");
    expect(canvas).toHaveAttribute("data-nodes-connectable", "false");
    expect(screen.queryByText("Pipeline")).not.toBeInTheDocument();
    await waitFor(() => expect(document.querySelector(".react-flow__attribution")).toBeInTheDocument());
  });

  it("renders a selected run trace and opens the node inspector", async () => {
    renderApp("/runs/synthetic-001/fixture-partial?tab=workflow");
    const status = await screen.findByLabelText("Workflow status summary");
    expect(status).toHaveTextContent("degraded");
    expect(status).toHaveTextContent("not evaluated");
    expect(screen.getAllByText("not evaluated").length).toBeGreaterThan(0);
    await waitFor(()=>expect(document.querySelector('[data-workflow-node-id="agent"]')).toBeInTheDocument());
    fireEvent.click(document.querySelector('[data-workflow-node-id="agent"]') as HTMLElement);
    expect(await screen.findByRole("heading", { name: "Agent harness" })).toBeInTheDocument();
  });

  it("renders answer-run scoring and the final answer artifact", async () => {
    const answerRun={...structuredClone(maxTurnRun),case_id:"answer-a",case_type:"landb_answer" as const,run_id:"answer-r1",resolved:true,patch_captured:false,partial_patch_captured:false,patch_applied:null,evaluation_skipped:false,mcp_condition:"curated",mcp_tool_call_count:1,artifacts:{...maxTurnRun.artifacts,answer:'{"answer":{"name":"device-a"}}',agent_patch:null,test_log:null},raw_sanitized_metadata:{condition:"curated",answer_correct:true,answer_format_correct:true,read_only:true}};
    vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL)=>{
      const path=String(input);const payload=path.includes("manifest")?{...manifest,case_count:1,run_count:1}:path.includes("runs/answer-a/answer-r1")?answerRun:path.includes("dashboard")?{...dashboard,case_count:1,run_count:1,runs:[answerRun]}:null;
      return payload?new Response(JSON.stringify(payload),{status:200,headers:{"Content-Type":"application/json"}}):new Response("missing",{status:404});
    }));
    renderApp("/runs/answer-a/answer-r1?tab=answer");
    expect(await screen.findByText("answer correct")).toBeInTheDocument();
    expect(screen.getByText("JSON valid · read-only yes")).toBeInTheDocument();
    expect(screen.getByRole("tab",{name:"Answer"})).toBeInTheDocument();
    expect(screen.queryByRole("tab",{name:"Agent patch"})).not.toBeInTheDocument();
    expect(screen.getByText(/device-a/)).toBeInTheDocument();
  });

  it("keeps workflow health distinct from a deterministic tests-failed outcome", async () => {
    const failedRun = structuredClone(maxTurnRun);
    failedRun.run_id = "tests-failed";
    failedRun.workflow.health = "healthy";
    failedRun.workflow.task_outcome = "unresolved";
    failedRun.workflow.nodes = failedRun.workflow.nodes.map((item) => item.id === "evaluation" ? { ...item, status: "error", status_reason: "Tests failed." } : item);
    failedRun.workflow.diagnostics = [{ severity:"info",code:"EVALUATION_TESTS_FAILED",message:"Tests failed without an infrastructure crash.",node_id:"evaluation",evidence:{field:"failure_mode",value:"tests_failed"},inspection_target:"Test log" }];
    const customDashboard = { ...dashboard, runs: [Object.fromEntries(Object.entries(failedRun).filter(([key]) => !["artifacts","raw_sanitized_metadata"].includes(key)))] };
    vi.stubGlobal("fetch", vi.fn(async (input:RequestInfo|URL)=>new Response(JSON.stringify(String(input).includes("manifest")?manifest:String(input).includes("runs/")?failedRun:customDashboard),{status:200,headers:{"Content-Type":"application/json"}})));
    renderApp("/runs/synthetic-001/tests-failed?tab=workflow");
    expect((await screen.findAllByText("healthy")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("unresolved").length).toBeGreaterThan(0);
    expect(screen.getByText("EVALUATION_TESTS_FAILED")).toBeInTheDocument();
    expect(screen.queryByText(/infrastructure crash/i)).toBeInTheDocument();
  });

  it("shows partial-patch and historical-state diagnostics plus a table fallback", async () => {
    renderApp("/runs/synthetic-001/fixture-partial?tab=workflow");
    expect(await screen.findByText("PARTIAL_PATCH_NOT_EVALUATED")).toBeInTheDocument();
    expect(screen.getByText("ARTIFACT_MISSING")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Accessible stage list" })).toBeInTheDocument();
    cleanup();
    renderApp("/runs/synthetic-001/historical?tab=workflow");
    expect(await screen.findByText("HISTORICAL_STATE_UNKNOWN")).toBeInTheDocument();
    expect(screen.getAllByText("unknown").length).toBeGreaterThan(0);
  });

  it("renders a derived running state without claiming authoritative liveness", async () => {
    const runningRun=structuredClone(maxTurnRun);runningRun.run_id="active";runningRun.workflow.health="running";runningRun.workflow.health_source="derived";runningRun.workflow.nodes=runningRun.workflow.nodes.map((item)=>item.id==="agent"?{...item,status:"running",status_reason:"Recent artifacts suggest activity; liveness is not proven."}:item);
    const activeDashboard={...dashboard,runs:[Object.fromEntries(Object.entries(runningRun).filter(([key])=>!["artifacts","raw_sanitized_metadata"].includes(key)))]};
    vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL)=>new Response(JSON.stringify(String(input).includes("manifest")?manifest:String(input).includes("runs/")?runningRun:activeDashboard),{status:200,headers:{"Content-Type":"application/json"}})));
    renderApp("/runs/synthetic-001/active?tab=workflow");
    expect((await screen.findAllByText("running")).length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Workflow status summary")).toHaveTextContent("derived");
  });

  it("isolates an unknown workflow schema inside the graph error boundary", async () => {
    vi.spyOn(console,"error").mockImplementation(()=>undefined);
    const badRun = structuredClone(maxTurnRun); badRun.workflow.schema_version = 99;
    const badDashboard = { ...dashboard, runs: [{ ...Object.fromEntries(Object.entries(badRun).filter(([key])=>!["artifacts","raw_sanitized_metadata"].includes(key))) }] };
    vi.stubGlobal("fetch", vi.fn(async (input:RequestInfo|URL)=>new Response(JSON.stringify(String(input).includes("manifest")?manifest:String(input).includes("runs/")?badRun:badDashboard),{status:200,headers:{"Content-Type":"application/json"}})));
    renderApp("/runs/synthetic-001/fixture-partial?tab=workflow");
    expect(await screen.findByText("This run contains invalid or incomplete workflow data.")).toBeInTheDocument();
    expect(screen.getByText(/Unsupported workflow schema 99/)).toBeInTheDocument();
    expect(screen.getAllByText("Diagnostics").length).toBeGreaterThan(0);
  });

  it("keeps execution traces inside run details", async () => {
    renderApp("/runs/synthetic-001/fixture-partial");
    const link = await screen.findByRole("link", { name: "Open execution trace for this run" });
    expect(link).toHaveAttribute("href", "/runs/synthetic-001/fixture-partial?tab=workflow");
    await userEvent.click(await screen.findByRole("tab", { name: "Execution" }));
    expect(screen.getByRole("tab", { name: "Execution trace" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Execution trace" }));
    expect(await screen.findByTestId("workflow-canvas")).toBeInTheDocument();
  });

  it("redirects legacy trace links to the matching run detail", async () => {
    renderApp("/workflow?mode=trace&case=synthetic-001&run=fixture-partial");
    expect(await screen.findByRole("heading",{name:"Run details"})).toBeInTheDocument();
    expect(await screen.findByTestId("workflow-canvas")).toBeInTheDocument();
  });

  it("can reuse an existing run configuration in Build", async () => {
    renderApp("/runs/synthetic-001/fixture-partial");
    await userEvent.click(await screen.findByRole("button",{name:"Use config in Build"}));
    expect(await screen.findByRole("heading",{name:"Build"})).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("patchbench.workflow.config.v1")??"{}")).toMatchObject({caseId:"synthetic-001",runId:"fixture-partial-repeat"});
  });

  it("opens the exact prompt from an indexed run directly from Prompt Setup", async () => {
    localStorage.setItem(WORKFLOW_CONFIG_STORAGE_KEY,JSON.stringify({...defaultWorkflowConfig("synthetic-001"),runId:"fixture-partial"}));
    renderApp("/workflow");
    await screen.findByRole("heading",{name:"Build"});
    fireEvent.click(document.querySelector('[data-build-node-id="prompt"] button') as HTMLElement);
    expect(await screen.findByText("Exact prompt sent to the model")).toBeInTheDocument();
    expect(await screen.findByText("Fix the observed behavior.")).toBeInTheDocument();
  });

  it("does not expose layout mutation controls for a stored trace", async () => {
    const key="patchbench.workflow.v1.trace.synthetic-001.fixture-partial"; localStorage.setItem(key,JSON.stringify({agent:{x:1,y:2}}));
    renderApp("/runs/synthetic-001/fixture-partial?tab=workflow");
    await screen.findByTestId("workflow-canvas");
    expect(screen.queryByRole("button", { name: "Reset workflow layout" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Fit workflow to view" })).not.toBeInTheDocument();
    expect(localStorage.getItem(key)).not.toBeNull();
  });

  it("renders the offline BUILD canvas with fixed config nodes and a truthful fallback", async () => {
    localStorage.setItem(WORKFLOW_CONFIG_STORAGE_KEY,JSON.stringify({...defaultWorkflowConfig("synthetic-001"),model:"fixture-model"}));
    renderApp("/workflow");
    expect(await screen.findByRole("heading",{name:"Build"})).toBeInTheDocument();
    expect(screen.queryByRole("radio",{name:"Run trace"})).not.toBeInTheDocument();
    const canvas=await screen.findByTestId("build-workflow-canvas");
    expect(canvas).toHaveAttribute("data-nodes-draggable","true");
    expect(canvas).toHaveAttribute("data-nodes-connectable","true");
    expect(document.querySelector('[data-build-node-id="case"]')).toBeInTheDocument();
    expect(document.querySelector('[data-build-node-id="mcp"]')).toBeInTheDocument();
    expect(document.querySelector('[data-build-node-id="command"]')).toBeInTheDocument();
    fireEvent.click(document.querySelector('[data-command-copy="true"]') as HTMLElement);
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(expect.stringContaining("patchbench run"));
    fireEvent.click(document.querySelector('[data-stage-inspect="worktree"]') as HTMLElement);
    expect(screen.getByRole("heading",{name:"Worktree details"})).toBeInTheDocument();
    expect(screen.getByText("This stage has not started. Run the benchmark to collect events.")).toBeInTheDocument();
    expect(await screen.findByRole("button",{name:"Play demo"})).toBeInTheDocument();
    expect(screen.getByRole("button",{name:"Copy real command"})).toBeInTheDocument();
    expect(screen.getByText(/Demo only/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button",{name:"Play demo"}));
    expect((await screen.findAllByText("Loading and validating the benchmark case metadata.")).length).toBeGreaterThan(0);
    expect(screen.getByText("Backend log")).toBeInTheDocument();
    expect(screen.getByText("SIMULATED")).toBeInTheDocument();
    expect(screen.getByRole("heading",{name:"Environment details"})).toBeInTheDocument();
    await waitFor(()=>expect(document.querySelector('[data-build-node-id="case"]')).toHaveAttribute("data-build-node-status","success"));
    expect(document.querySelector('[data-build-node-id="command"]')).toHaveAttribute("data-build-node-status","success");
    expect(canvas).toHaveAttribute("data-input-edges-active","true");
    expect(screen.getByText("Inputs flowing → command")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button",{name:"Cancel run"}));
    expect(await screen.findByText("Demo stopped")).toBeInTheDocument();
  });

  it("switches Build to answer-specific controls for a LanDB case", async () => {
    const answerCase={...dashboard.cases[0],case_id:"answer-a",case_type:"landb_answer" as const,allowed_operations:["validate-case","run-landb-answer","verify-landb-dev"] as const,repository:null,evaluation:{...dashboard.cases[0].evaluation,test_command:null,timeout_seconds:600}};
    const answerDashboard={...dashboard,case_count:1,cases:[answerCase],runs:[]};
    vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL)=>{
      const path=String(input);const payload=path.includes("manifest")?{...manifest,case_count:1,run_count:0}:path.includes("dashboard")?answerDashboard:null;
      return payload?new Response(JSON.stringify(payload),{status:200,headers:{"Content-Type":"application/json"}}):new Response("missing",{status:404});
    }));
    renderApp("/workflow");
    expect(await screen.findByRole("button",{name:"Play answer demo"})).toBeInTheDocument();
    fireEvent.click(document.querySelector('[data-build-node-id="prompt"] button') as HTMLElement);
    expect(await screen.findByText("Experimental condition")).toBeInTheDocument();
    expect(screen.getByText("condition · curated")).toBeInTheDocument();
  });

  it("requires a named model and offers recent names without a provider default", async () => {
    renderApp("/workflow");
    await screen.findByRole("heading",{name:"Build"});
    expect(await screen.findByLabelText("Recent model")).toHaveAttribute("data-model-options",expect.stringContaining("fixture-model"));
    expect(screen.queryByText("provider default")).not.toBeInTheDocument();
    expect(screen.getByRole("button",{name:"Play demo"})).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Model ID"),{target:{value:"fixture-model"}});
    expect(screen.getByRole("button",{name:"Play demo"})).toBeEnabled();
    expect(screen.getByText("Model:").parentElement).toHaveTextContent("fixture-model");
  });

  it("offers selectable container image, credential, and CA presets", async () => {
    localStorage.setItem(WORKFLOW_CONFIG_STORAGE_KEY,JSON.stringify({...defaultWorkflowConfig("synthetic-001"),container:true}));
    renderApp("/workflow");
    await screen.findByRole("heading",{name:"Build"});
    fireEvent.click(document.querySelector('[data-build-node-id="runtime"] button') as HTMLElement);

    expect(await screen.findByLabelText("Agent image")).toHaveAttribute("data-options",expect.stringContaining("patchbench-rucio-claude:tests-v2"));
    expect(screen.getByLabelText("Agent env file")).toHaveAttribute("data-options",expect.stringContaining("~/.config/patchbench/claude-env.sh"));
    expect(screen.getByLabelText("Agent env file")).toHaveClass("overflow-hidden");
    expect(screen.getByLabelText("CA file")).toHaveAttribute("data-options",expect.stringContaining("~/.config/patchbench/provider-ca-bundle.pem"));
    expect(screen.getByLabelText("Custom agent env file")).toBeInTheDocument();
    expect(screen.getByLabelText("Custom ca file")).toBeInTheDocument();
  });

  it("shows every reported live phase and the exact solver prompt", async () => {
    localStorage.setItem(WORKFLOW_CONFIG_STORAGE_KEY,JSON.stringify({...defaultWorkflowConfig("synthetic-001"),model:"fixture-model"}));
    const dataFetch=vi.mocked(fetch);
    vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
      const path=String(input);
      if(path.endsWith("/api/health"))return new Response(JSON.stringify({version:"test",schema_version:1,capabilities:["run"],active_runs:[]}),{status:200,headers:{"Content-Type":"application/json"}});
      if(path.endsWith("/api/runs")&&init?.method==="POST")return new Response(JSON.stringify({run_id:"all-phases"}),{status:202,headers:{"Content-Type":"application/json"}});
      if(path.endsWith("/api/runs/all-phases/events"))return new Response([
        JSON.stringify({seq:0,type:"run_start",run_id:"all-phases",case_id:"synthetic-001",status:"queued"}),
        JSON.stringify({seq:1,type:"phase",phase:"environment",status:"success",last_activity:"environment complete"}),
        JSON.stringify({seq:2,type:"phase",phase:"prompt",status:"success",prompt:"EXACT MODEL PROMPT",last_activity:"prompt complete"}),
        ...["worktree","agent","tools","patch","termination","evaluation","result"].map((phase,index)=>JSON.stringify({seq:index+3,type:"phase",phase,status:"success",last_activity:`${phase} complete`})),
        JSON.stringify({seq:10,type:"run_complete",resolved:true}),
        "",
      ].join("\n"),{status:200,headers:{"Content-Type":"application/x-ndjson"}});
      return dataFetch(input,init);
    }));

    renderApp("/workflow");
    await userEvent.click(await screen.findByRole("button",{name:"Run all"}));
    await waitFor(()=>["environment","prompt-stage","worktree","agent-stage","tools","patch","termination","evaluation","result"].forEach((id)=>expect(document.querySelector(`[data-build-node-id="${id}"]`)).toHaveAttribute("data-build-node-status","success")));
    expect(screen.getByRole("progressbar",{name:"Workflow completion"})).toHaveAttribute("aria-valuenow","100");
    fireEvent.click(document.querySelector('[data-build-node-id="prompt"] button') as HTMLElement);
    expect(await screen.findByText("Exact prompt sent to the model")).toBeInTheDocument();
    expect(screen.getByText("EXACT MODEL PROMPT")).toBeInTheDocument();
  });

  it("derives completed prompt and worktree stages when an older stream resumes at the agent", async () => {
    localStorage.setItem(WORKFLOW_CONFIG_STORAGE_KEY,JSON.stringify({...defaultWorkflowConfig("synthetic-001"),model:"fixture-model"}));
    const dataFetch=vi.mocked(fetch);
    vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
      const path=String(input);
      if(path.endsWith("/api/health"))return new Response(JSON.stringify({version:"test",schema_version:1,capabilities:["run"],active_runs:[]}),{status:200,headers:{"Content-Type":"application/json"}});
      if(path.endsWith("/api/runs")&&init?.method==="POST")return new Response(JSON.stringify({run_id:"sparse-phases"}),{status:202,headers:{"Content-Type":"application/json"}});
      if(path.endsWith("/api/runs/sparse-phases/events"))return new Response([
        JSON.stringify({seq:0,type:"run_start",run_id:"sparse-phases",case_id:"synthetic-001",status:"queued"}),
        JSON.stringify({seq:1,type:"phase",phase:"environment",status:"success"}),
        JSON.stringify({seq:2,type:"phase",phase:"agent",status:"running",last_activity:"Agent started"}),
        JSON.stringify({seq:3,type:"phase",phase:"analysis",status:"running",heartbeat:true}),
        JSON.stringify({seq:4,type:"phase",phase:"analysis",status:"running",last_activity:"Provider connection retry 2 of 10; next attempt in 1.1s"}),
        "",
      ].join("\n"),{status:200,headers:{"Content-Type":"application/x-ndjson"}});
      if(path.endsWith("/api/runs/sparse-phases/prompt"))return new Response(JSON.stringify({run_id:"sparse-phases",prompt:"PROMPT RECOVERED FROM RUN"}),{status:200,headers:{"Content-Type":"application/json"}});
      return dataFetch(input,init);
    }));

    renderApp("/workflow");
    await userEvent.click(await screen.findByRole("button",{name:"Run all"}));
    await waitFor(()=>{
      expect(document.querySelector('[data-build-node-id="prompt-stage"]')).toHaveAttribute("data-build-node-status","success");
      expect(document.querySelector('[data-build-node-id="worktree"]')).toHaveAttribute("data-build-node-status","success");
      expect(document.querySelector('[data-build-node-id="agent-stage"]')).toHaveAttribute("data-build-node-status","running");
    });
    const progress=screen.getByLabelText("Run progress");
    expect(within(progress).getByText("Running")).toBeInTheDocument();
    expect(within(progress).getByText("Agent activity")).toBeInTheDocument();
    expect(within(progress).getByText("Provider connection retry 2 of 10; next attempt in 1.1s")).toBeInTheDocument();
    expect(within(progress).getByText("3 updates")).toBeInTheDocument();
    const activity=screen.getByLabelText("Run activity status");
    expect(within(activity).getByText(/Elapsed/)).toBeInTheDocument();
    expect(within(activity).getByText(/Agent time limit remaining/)).toBeInTheDocument();
    fireEvent.click(document.querySelector('[data-build-node-id="prompt"] button') as HTMLElement);
    expect(await screen.findByText("PROMPT RECOVERED FROM RUN")).toBeInTheDocument();
  });

  it("validates a case through the local service and refreshes its status", async () => {
    const dataFetch=vi.mocked(fetch);let submitted:Record<string,unknown>|null=null;
    vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
      const path=String(input);
      if(path.endsWith("/api/cases/synthetic-001/validate")&&init?.method==="POST"){
        submitted=JSON.parse(String(init.body)) as Record<string,unknown>;
        return new Response(JSON.stringify({result:{case_id:"synthetic-001",validated:true,error:null},dashboard_updated:true}),{status:200,headers:{"Content-Type":"application/json"}});
      }
      return dataFetch(input,init);
    }));

    renderApp("/cases");
    await userEvent.click(await screen.findByRole("button",{name:"Open case"}));
    expect(await screen.findByLabelText("Case validation")).toBeInTheDocument();
    expect(screen.getByLabelText("Validate in Podman")).not.toBeChecked();
    await userEvent.click(screen.getByRole("button",{name:"Validate case"}));
    expect(await screen.findByText("Case validation passed and the dashboard was refreshed.")).toBeInTheDocument();
    expect(submitted).toMatchObject({case_id:"synthetic-001",container:false,landb_mcp_repo:"../landb-mcp"});
  });

  it("runs patch evaluation through the generic operation stream", async () => {
    const dataFetch=vi.mocked(fetch);let submitted:Record<string,unknown>|null=null;
    vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
      const path=String(input);
      if(path.endsWith("/api/operations")&&init?.method==="POST"){
        submitted=JSON.parse(String(init.body)) as Record<string,unknown>;
        return new Response(JSON.stringify({operation_id:"evaluate-patch-test",kind:"evaluate-patch"}),{status:202,headers:{"Content-Type":"application/json"}});
      }
      if(path.endsWith("/api/operations/evaluate-patch-test/events"))return new Response([
        JSON.stringify({seq:0,type:"operation_start",status:"queued"}),
        JSON.stringify({seq:1,type:"operation_status",status:"running"}),
        JSON.stringify({seq:2,type:"log",message:"Patch evaluation completed"}),
        JSON.stringify({seq:3,type:"operation_complete",status:"success",result:{patch_applied:true,resolved:true}}),
        "",
      ].join("\n"),{status:200,headers:{"Content-Type":"application/x-ndjson"}});
      return dataFetch(input,init);
    }));

    renderApp("/workflow");
    await userEvent.click(await screen.findByRole("tab",{name:"Backend operations"}));
    expect(await screen.findByRole("heading",{name:"Backend operations"})).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Patch content"),{target:{value:"diff --git a/a b/a\n"}});
    await userEvent.click(screen.getByRole("button",{name:"Evaluate patch"}));
    expect(await screen.findByText("Patch evaluation completed")).toBeInTheDocument();
    expect(screen.getByText(/"resolved": true/)).toBeInTheDocument();
    expect(submitted).toMatchObject({kind:"evaluate-patch",case_id:"synthetic-001",patch_text:"diff --git a/a b/a\n"});
  });

  it("reconnects to an active backend operation after a frontend reload", async () => {
    localStorage.setItem("patchbench.operation.active","report-resume");
    const dataFetch=vi.mocked(fetch);let operationPosts=0;
    vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
      const path=String(input);
      if(path.endsWith("/api/operations")&&init?.method==="POST")operationPosts+=1;
      if(path.endsWith("/api/operations/report-resume/events"))return new Response([
        JSON.stringify({seq:5,type:"log",message:"Report resumed"}),
        JSON.stringify({seq:6,type:"operation_complete",status:"success",result:{report:"complete"}}),
        "",
      ].join("\n"),{status:200,headers:{"Content-Type":"application/x-ndjson"}});
      return dataFetch(input,init);
    }));

    renderApp("/workflow");
    await userEvent.click(await screen.findByRole("tab",{name:"Backend operations"}));

    expect(await screen.findByText("Report resumed")).toBeInTheDocument();
    expect(screen.getByText(/"report": "complete"/)).toBeInTheDocument();
    expect(operationPosts).toBe(0);
    expect(localStorage.getItem("patchbench.operation.active")).toBeNull();
  });

  it("recovers the Build controls when the live run request fails", async () => {
    localStorage.setItem(WORKFLOW_CONFIG_STORAGE_KEY,JSON.stringify({...defaultWorkflowConfig("synthetic-001"),model:"fixture-model"}));
    const dataFetch=vi.mocked(fetch);
    vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
      const path=String(input);
      if(path.endsWith("/api/health"))return new Response(JSON.stringify({version:"test",schema_version:1,capabilities:["run"],active_runs:[]}),{status:200,headers:{"Content-Type":"application/json"}});
      if(path.endsWith("/api/runs")&&init?.method==="POST")throw new TypeError("network down");
      return dataFetch(input,init);
    }));
    renderApp("/workflow");
    const runButton=await screen.findByRole("button",{name:"Run all"});
    await userEvent.click(runButton);
    expect(await screen.findByText("network down")).toBeInTheDocument();
    expect(screen.queryByRole("button",{name:"Cancel run"})).not.toBeInTheDocument();
    expect(screen.getByRole("button",{name:"Run all"})).toBeEnabled();
  });

  it("refreshes exported data and exposes an inspect link after indexing", async () => {
    localStorage.setItem(WORKFLOW_CONFIG_STORAGE_KEY,JSON.stringify({...defaultWorkflowConfig("synthetic-001"),model:"fixture-model"}));
    const dataFetch=vi.mocked(fetch);let dashboardRequests=0;
    vi.stubGlobal("fetch",vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
      const path=String(input);
      if(path.endsWith("/api/health"))return new Response(JSON.stringify({version:"test",schema_version:1,capabilities:["run","dashboard-indexing"],active_runs:[]}),{status:200,headers:{"Content-Type":"application/json"}});
      if(path.endsWith("/api/runs")&&init?.method==="POST")return new Response(JSON.stringify({run_id:"indexed-run"}),{status:202,headers:{"Content-Type":"application/json"}});
      if(path.endsWith("/api/runs/indexed-run/events"))return new Response([
        JSON.stringify({seq:0,type:"run_start",run_id:"indexed-run",case_id:"synthetic-001",status:"queued"}),
        JSON.stringify({seq:1,type:"dashboard_updated",phase:"result",status:"success",run_id:"indexed-run",case_id:"synthetic-001",run_indexed:true}),
        JSON.stringify({seq:2,type:"run_complete",resolved:true}),
        "",
      ].join("\n"),{status:200,headers:{"Content-Type":"application/x-ndjson"}});
      if(path.endsWith("/data/dashboard.json"))dashboardRequests+=1;
      return dataFetch(input,init);
    }));

    renderApp("/workflow");
    await userEvent.click(await screen.findByRole("button",{name:"Run all"}));
    const inspect=await screen.findByRole("link",{name:"Inspect new run"});
    expect(inspect).toHaveAttribute("href","/runs/synthetic-001/indexed-run");
    await waitFor(()=>expect(dashboardRequests).toBeGreaterThanOrEqual(2));
  });

  it("disables active edge animation for reduced-motion users", async () => {
    vi.mocked(window.matchMedia).mockImplementation((query:string)=>({matches:query.includes("prefers-reduced-motion"),media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}));
    renderApp("/runs/synthetic-001/fixture-partial?tab=workflow");
    await screen.findByTestId("workflow-canvas");
    await waitFor(()=>expect(document.querySelector(".react-flow__edge.animated")).toBeNull());
  });
});
