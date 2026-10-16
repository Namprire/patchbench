import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useDashboard } from "../data/dashboard";
import { RunFilters, emptyRunFilters, filterRuns, type RunFilterState } from "../components/patchbench/run-filters";
import { RunTable } from "../components/patchbench/run-table";
import { EmptyState } from "../components/patchbench/empty-state";
import { PageError, PageHeading, PageLoading } from "./page-state";

function filtersFromParams(params:URLSearchParams):RunFilterState {
  return {...emptyRunFilters,
    search:params.get("search")??"", caseId:params.get("case")??"all", model:params.get("model")??"all", harness:params.get("harness")??"all", promptMode:params.get("promptMode")??"all", category:params.get("category")??"all", termination:params.get("termination")??"all", evaluation:params.get("evaluation")??"all",
    resolved:params.get("resolved")==="true"?"yes":params.get("resolved")==="false"?"no":"all",
    partial:params.get("partialPatch")==="true"?"yes":params.get("partialPatch")==="false"?"no":"all",
    patchApplied:params.get("patchApplied")==="true"?"yes":params.get("patchApplied")==="false"?"no":"all",
  };
}

export function RunsPage() {
  const { dashboard, loading, error } = useDashboard();
  const [params]=useSearchParams();
  const paramsKey=params.toString();
  const [filters,setFilters]=useState<RunFilterState>(()=>filtersFromParams(params));
  const [density,setDensity]=useState<"comfortable"|"dense">(()=>localStorage.getItem("patchbench.runs.density")==="dense"?"dense":"comfortable");
  useEffect(()=>setFilters(filtersFromParams(new URLSearchParams(paramsKey))),[paramsKey]);
  useEffect(()=>localStorage.setItem("patchbench.runs.density",density),[density]);
  const runs=useMemo(()=>dashboard?.runs??[],[dashboard]);
  const filtered=useMemo(()=>filterRuns(runs,filters),[runs,filters]);
  if(loading)return <PageLoading/>; if(error)return <PageError message={error}/>; if(!dashboard)return <EmptyState/>;
  return <><PageHeading eyebrow="Coding agent evaluation" title="Runs" description="Search, filter, and compare exported benchmark runs."/><RunFilters runs={runs} value={filters} onChange={setFilters} resultCount={filtered.length}/><div className="mt-3"><RunTable runs={filtered} density={density} onDensityChange={setDensity} onClearFilters={()=>setFilters(emptyRunFilters)} initialSort={params.get("sort")??undefined}/></div></>;
}
