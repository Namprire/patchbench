import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { DASHBOARD_SCHEMA_VERSION, type CaseDetail, type DashboardData, type Manifest, type RunDetail } from "../types/dashboard";

type State = { manifest: Manifest | null; dashboard: DashboardData | null; loading: boolean; refreshing: boolean; error: string | null; refreshError: string | null; refresh: () => Promise<void>; loadRun: (caseId: string, runId: string, signal?: AbortSignal) => Promise<RunDetail>; loadCase: (caseId: string, signal?: AbortSignal) => Promise<CaseDetail> };
const DashboardContext = createContext<State | null>(null);

function assertVersion(payload: { schema_version?: number }): void {
  if (payload.schema_version !== DASHBOARD_SCHEMA_VERSION) throw new Error(`Unsupported dashboard schema ${payload.schema_version ?? "missing"}; this console supports schema ${DASHBOARD_SCHEMA_VERSION}.`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function assertPayloadShape(path: string, payload: unknown): asserts payload is { schema_version?: number } {
  if (!isRecord(payload)) throw new Error(`Invalid dashboard data in ${path}: expected an object.`);
  if (path.endsWith("/manifest.json")) {
    if (typeof payload.case_count !== "number" || typeof payload.run_count !== "number") throw new Error(`Invalid dashboard manifest in ${path}.`);
    return;
  }
  if (path.endsWith("/dashboard.json")) {
    if (!Array.isArray(payload.cases) || !Array.isArray(payload.runs)) throw new Error(`Invalid dashboard index in ${path}: cases and runs must be arrays.`);
    return;
  }
  if (path.includes("/runs/")) {
    if (typeof payload.case_id !== "string" || typeof payload.run_id !== "string" || !Array.isArray(payload.files_changed) || !isRecord(payload.artifacts) || !isRecord(payload.workflow)) throw new Error(`Invalid run detail in ${path}.`);
    return;
  }
  if (path.includes("/cases/") && (typeof payload.case_id !== "string" || !Array.isArray(payload.run_ids))) throw new Error(`Invalid case detail in ${path}.`);
}

async function fetchJson<T extends { schema_version?: number }>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { cache: "no-store", signal });
  if (!response.ok) throw new Error(`Unable to load ${path} (${response.status})`);
  const payload: unknown = await response.json();
  assertPayloadShape(path, payload);
  assertVersion(payload);
  return payload as T;
}

export function DashboardProvider({ children }: { children: ReactNode }) {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(null);
    Promise.all([fetchJson<Manifest>("/data/manifest.json", controller.signal), fetchJson<DashboardData>("/data/dashboard.json", controller.signal)])
      .then(([nextManifest, nextDashboard]) => { setManifest(nextManifest); setDashboard(nextDashboard); })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Unable to load dashboard data"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);

  const refresh = useCallback(async () => {
    if (refreshing) return;
    setRefreshing(true); setRefreshError(null);
    try {
      const [nextManifest, nextDashboard] = await Promise.all([fetchJson<Manifest>("/data/manifest.json"), fetchJson<DashboardData>("/data/dashboard.json")]);
      setManifest(nextManifest); setDashboard(nextDashboard); setError(null);
    } catch (reason: unknown) {
      const message = reason instanceof Error ? reason.message : "Unable to refresh dashboard data";
      setRefreshError(message);
      throw reason;
    } finally {
      setRefreshing(false);
    }
  }, [refreshing]);

  const loadRun = useCallback((caseId: string, runId: string, signal?: AbortSignal) => fetchJson<RunDetail>(`/data/runs/${encodeURIComponent(caseId)}/${encodeURIComponent(runId)}.json`, signal), []);
  const loadCase = useCallback((caseId: string, signal?: AbortSignal) => fetchJson<CaseDetail>(`/data/cases/${encodeURIComponent(caseId)}.json`, signal), []);
  const value = useMemo(() => ({ manifest, dashboard, loading, refreshing, error, refreshError, refresh, loadRun, loadCase }), [manifest, dashboard, loading, refreshing, error, refreshError, refresh, loadRun, loadCase]);
  return <DashboardContext.Provider value={value}>{children}</DashboardContext.Provider>;
}

export function useDashboard() { const value = useContext(DashboardContext); if (!value) throw new Error("useDashboard must be used within DashboardProvider"); return value; }

type ComparisonState = { selected: string[]; toggle: (caseId: string, runId: string) => void; remove: (caseId: string, runId: string) => void; clear: () => void; contains: (caseId: string, runId: string) => boolean };
const ComparisonContext = createContext<ComparisonState | null>(null);
export function ComparisonProvider({ children }: { children: ReactNode }) { const [selected, setSelected] = useState<string[]>([]); const key = (caseId: string, runId: string) => `${caseId}/${runId}`; const toggle = useCallback((caseId: string, runId: string) => setSelected((current) => { const value = key(caseId, runId); return current.includes(value) ? current.filter((item) => item !== value) : current.length < 4 ? [...current, value] : current; }), []); const remove = useCallback((caseId: string, runId: string) => setSelected((current) => current.filter((item) => item !== key(caseId, runId))), []); const clear = useCallback(() => setSelected([]), []); const contains = useCallback((caseId: string, runId: string) => selected.includes(key(caseId, runId)), [selected]); const value = useMemo(() => ({ selected, toggle, remove, clear, contains }), [selected, toggle, remove, clear, contains]); return <ComparisonContext.Provider value={value}>{children}</ComparisonContext.Provider>; }
export function useComparison() { const value = useContext(ComparisonContext); if (!value) throw new Error("useComparison must be used within ComparisonProvider"); return value; }
