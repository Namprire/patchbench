export type RunStop = "prompt" | "patch" | "evaluation";
export const WORKFLOW_CONFIG_STORAGE_KEY = "patchbench.workflow.config.v1";

export interface WorkflowConfig {
  caseId: string;
  caseType: "coding" | "landb_answer";
  agent: string;
  runId: string;
  model: string;
  promptMode: string;
  maxTurns: number;
  timeout: number;
  container: boolean;
  live: boolean;
  debug: boolean;
  image: string;
  agentCommand: string;
  network: string;
  envFile: string;
  caFile: string;
  mcpServer: string;
  mcpCommand: string;
  mcpProfile: string;
  mcpEnvFile: string;
  answerCondition: "none" | "curated" | "broad";
  liveDev: boolean;
  landbMcpRepo: string;
  artifactsRoot: string;
}

export type CommandValues = Partial<Omit<WorkflowConfig, "caseId" | "agent" | "runId" | "promptMode" | "maxTurns" | "timeout" | "container" | "live" | "debug">> &
  Pick<WorkflowConfig, "caseId" | "agent" | "runId" | "promptMode" | "maxTurns" | "timeout" | "container" | "live" | "debug">;

export const defaultWorkflowConfig = (caseId = "demo-label"): WorkflowConfig => ({
  caseId,
  caseType: "coding",
  agent: "claude-code",
  runId: `${caseId.replace(/^cfmgr-/, "run-")}-hints-100t-r1`,
  model: "",
  promptMode: "issue_with_hints",
  maxTurns: 100,
  timeout: 900,
  container: false,
  live: true,
  debug: false,
  image: "",
  agentCommand: "",
  network: "pasta",
  envFile: "",
  caFile: "",
  mcpServer: "none",
  mcpCommand: "",
  mcpProfile: "curated",
  mcpEnvFile: "",
  answerCondition: "curated",
  liveDev: false,
  landbMcpRepo: "../landb-mcp",
  artifactsRoot: "",
});

const shellQuote = (value: string) => `'${value.replaceAll("'", `'"'"'`)}'`;

export function buildCommand(values: CommandValues, stopAt: RunStop = "evaluation") {
  const model = values.model?.trim();
  if (values.caseType === "landb_answer") {
    const args = ["patchbench run-landb-answer", shellQuote(values.caseId), "\\\n  --condition", shellQuote(values.answerCondition ?? "curated"), "\\\n  --run-id", shellQuote(values.runId), "\\\n  --landb-mcp-repo", shellQuote(values.landbMcpRepo ?? "../landb-mcp"), "\\\n  --timeout-seconds", String(values.timeout), "\\\n  --agent-max-turns", String(values.maxTurns)];
    if (model) args.push("\\\n  --agent-model", shellQuote(model));
    if (values.envFile) args.push("\\\n  --agent-env-file", shellQuote(values.envFile));
    if (values.liveDev) {
      args.push("\\\n  --live-dev");
      if (values.mcpEnvFile) args.push("\\\n  --mcp-env-file", shellQuote(values.mcpEnvFile));
      if (values.artifactsRoot) args.push("\\\n  --artifacts-root", shellQuote(values.artifactsRoot));
    }
    return args.join(" ");
  }
  const args = ["patchbench run", shellQuote(values.caseId), "\\\n  --agent", shellQuote(values.agent), "\\\n  --run-id", shellQuote(values.runId)];
  if (values.agent === "claude-code" && model) args.push("\\\n  --agent-model", shellQuote(model));
  if (values.agent === "claude-code") args.push("\\\n  --agent-max-turns", String(values.maxTurns));
  args.push("\\\n  --agent-timeout-seconds", String(values.timeout));
  if (values.debug && values.agent === "claude-code") args.push("\\\n  --agent-debug");
  if (values.container) args.push("\\\n  --container");
  if (values.image) args.push("\\\n  --agent-image", shellQuote(values.image));
  if (values.agentCommand) args.push("\\\n  --agent-command", shellQuote(values.agentCommand));
  if (values.network && values.network !== "pasta") args.push("\\\n  --agent-network", shellQuote(values.network));
  if (values.envFile) args.push("\\\n  --agent-env-file", shellQuote(values.envFile));
  if (values.caFile) args.push("\\\n  --agent-ca-file", shellQuote(values.caFile));
  args.push("\\\n  --prompt-mode", shellQuote(values.promptMode));
  if (values.mcpServer && values.mcpServer !== "none") {
    args.push("\\\n  --mcp-server", shellQuote(values.mcpServer));
    if (values.mcpProfile) args.push("\\\n  --mcp-profile", shellQuote(values.mcpProfile));
    if (values.mcpCommand) args.push("\\\n  --mcp-command", shellQuote(values.mcpCommand));
    if (values.mcpEnvFile) args.push("\\\n  --mcp-env-file", shellQuote(values.mcpEnvFile));
  }
  if (stopAt === "prompt") args.push("\\\n  --dry-run");
  if (stopAt === "patch") args.push("\\\n  --skip-evaluation");
  if (values.live) args.push("\\\n  --live");
  return args.join(" ");
}

export function isValidRunId(value: string) {
  return Boolean(value) && value !== "." && value !== ".." && !value.includes("/") && !value.includes("\\");
}

export function workflowConfigError(config: WorkflowConfig, stopAt: RunStop = "evaluation"): string | null {
  if (!Number.isInteger(config.maxTurns) || config.maxTurns <= 0) return "Max turns must be a positive integer.";
  if (!Number.isInteger(config.timeout) || config.timeout <= 0) return "Timeout must be a positive integer.";
  if ((config.caseType === "landb_answer" || config.agent === "claude-code" && stopAt !== "prompt") && !config.model.trim()) return "Select or enter a model in the Agent node before running the benchmark.";
  if (config.caseType === "landb_answer") {
    if (!config.landbMcpRepo.trim()) return "LanDB MCP repository is required.";
    if (config.liveDev && !config.mcpEnvFile.trim()) return "CERN DEV requires an external MCP environment file.";
    if (config.liveDev && !config.artifactsRoot.trim()) return "CERN DEV requires an external artifacts root.";
  }
  if (config.agent === "claude-code" && config.container && !config.envFile.trim()) return "Containerized Claude Code requires an Agent env file. Select it in the Runtime node.";
  if (config.agent === "container-command" && !config.container) return "container-command requires the container runtime.";
  return null;
}

export function toRunRequest(config: WorkflowConfig, stopAt: RunStop) {
  if (config.caseType === "landb_answer") return {
    case_id: config.caseId,
    run_id: config.runId,
    agent: "claude-code",
    stop_at: "evaluation",
    answer_condition: config.answerCondition,
    landb_mcp_repo: config.landbMcpRepo,
    agent_env_file: config.envFile || null,
    agent_model: config.model.trim() || null,
    agent_timeout_seconds: config.timeout,
    agent_max_turns: config.maxTurns,
    container: false,
    live_dev: config.liveDev,
    mcp_env_file: config.liveDev ? config.mcpEnvFile || null : null,
    artifacts_root: config.liveDev ? config.artifactsRoot || null : null,
  };
  return {
    case_id: config.caseId,
    agent: config.agent,
    run_id: config.runId,
    prompt_mode: config.promptMode,
    agent_timeout_seconds: config.timeout,
    agent_model: config.model.trim() || null,
    agent_max_turns: config.maxTurns,
    agent_debug: config.debug,
    container: config.container,
    agent_env_file: config.envFile || null,
    agent_ca_file: config.caFile || null,
    agent_image: config.image || null,
    agent_command: config.agentCommand || null,
    agent_network: config.network,
    mcp_server: config.mcpServer === "none" ? null : config.mcpServer,
    mcp_command: config.mcpCommand || null,
    mcp_profile: config.mcpServer === "none" ? null : config.mcpProfile,
    mcp_env_file: config.mcpEnvFile || null,
    stop_at: stopAt,
    cli_equivalent: buildCommand(config, stopAt),
  };
}

export function configFromRun(run: {case_id:string;run_id:string;agent:string|null;requested_model:string|null;prompt_mode:string|null;configured_max_turns:number|null;timeout_seconds:number|null;mcp_server_name:string|null;mcp_profile:string|null;run_config?:Record<string,unknown>|null}): WorkflowConfig {
  const stored=run.run_config??{};const text=(key:string,fallback:string)=>typeof stored[key]==="string"?stored[key] as string:fallback;const positive=(key:string,fallback:number)=>typeof stored[key]==="number"&&Number.isFinite(stored[key])&&(stored[key] as number)>0?stored[key] as number:fallback;const flag=(key:string,fallback:boolean)=>typeof stored[key]==="boolean"?stored[key] as boolean:fallback;
  return {
    ...defaultWorkflowConfig(run.case_id),
    caseId: run.case_id,
    caseType: text("case_type","coding") === "landb_answer" ? "landb_answer" : "coding",
    runId: `${run.run_id}-repeat`,
    agent: text("agent",run.agent??"claude-code"),
    model: text("requested_model",run.requested_model??""),
    promptMode: text("prompt_mode",run.prompt_mode??"issue_only"),
    maxTurns: positive("agent_max_turns",run.configured_max_turns??100),
    timeout: positive("agent_timeout_seconds",run.timeout_seconds??900),
    container: flag("container",false),
    debug: flag("agent_debug",false),
    image: text("agent_image",""),
    agentCommand: "",
    network: text("agent_network","pasta"),
    mcpServer: text("mcp_server",run.mcp_server_name??"none"),
    mcpProfile: text("mcp_profile",run.mcp_profile??"curated"),
    answerCondition: (text("answer_condition","curated") as WorkflowConfig["answerCondition"]),
    liveDev: flag("live_dev",false),
  };
}
