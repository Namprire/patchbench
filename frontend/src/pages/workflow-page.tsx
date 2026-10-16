import { lazy, Suspense } from "react";
import { Navigate, useSearchParams } from "react-router-dom";
import { BuildWorkflow } from "../components/patchbench/workflow/build-workflow";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { useDashboard } from "../data/dashboard";
import { PageError, PageHeading, PageLoading } from "./page-state";

const BackendOperations=lazy(()=>import("./tools-page").then((module)=>({default:module.BackendOperations})));

export function WorkflowPage(){
  const{dashboard,loading,error,refresh,loadRun}=useDashboard();
  const[params]=useSearchParams();
  if(params.get("mode")==="trace"){
    const caseId=params.get("case");const runId=params.get("run");
    return <Navigate replace to={caseId&&runId?`/runs/${encodeURIComponent(caseId)}/${encodeURIComponent(runId)}?tab=workflow`:"/runs"}/>;
  }
  if(loading)return <PageLoading/>;if(error||!dashboard)return <PageError message={error??"Dashboard export unavailable"}/>;
  return <>
    <PageHeading eyebrow="Coordinate" title="Build" description="Configure and run benchmarks, then manage the supporting backend operations from one workspace."/>
    <Tabs defaultValue="run">
      <TabsList aria-label="Build workspace sections"><TabsTrigger value="run">Run workflow</TabsTrigger><TabsTrigger value="operations">Backend operations</TabsTrigger></TabsList>
      <TabsContent value="run"><BuildWorkflow dashboard={dashboard} loadRun={loadRun} onCompleted={()=>void refresh().catch(()=>{/* A later manual refresh can retry the export. */})}/></TabsContent>
      <TabsContent value="operations"><Suspense fallback={<PageLoading/>}><BackendOperations dashboard={dashboard} refresh={refresh}/></Suspense></TabsContent>
    </Tabs>
  </>;
}
