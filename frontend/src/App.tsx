import { lazy, Suspense, useEffect } from "react";
import { Route, Routes, useLocation } from "react-router-dom";
import { AppShell } from "./components/patchbench/app-shell";
import { CompareTray } from "./components/patchbench/compare-tray";
import { Toaster } from "./components/ui/sonner";
import { PageLoading } from "./pages/page-state";

const OverviewPage = lazy(() => import("./pages/overview-page").then((module) => ({ default: module.OverviewPage })));
const RunsPage = lazy(() => import("./pages/runs-page").then((module) => ({ default: module.RunsPage })));
const RunDetailPage = lazy(() => import("./pages/run-detail-page").then((module) => ({ default: module.RunDetailPage })));
const ComparePage = lazy(() => import("./pages/compare-page").then((module) => ({ default: module.ComparePage })));
const CasesPage = lazy(() => import("./pages/cases-page").then((module) => ({ default: module.CasesPage })));
const CommandBuilderPage = lazy(() => import("./pages/command-builder-page").then((module) => ({ default: module.CommandBuilderPage })));
const loadWorkflowPage = () => import("./pages/workflow-page").then((module) => ({ default: module.WorkflowPage }));
const WorkflowPage = lazy(loadWorkflowPage);
const NotFoundPage = lazy(() => import("./pages/not-found-page").then((module) => ({ default: module.NotFoundPage })));

export function App() {
  const {pathname}=useLocation();
  useEffect(()=>{const timer=window.setTimeout(()=>void loadWorkflowPage(),100);return()=>window.clearTimeout(timer)},[]);
  const consoleRoute=pathname==="/workflow"||/^\/runs\/[^/]+\/[^/]+$/.test(pathname);
  return <>
    <Suspense fallback={<div className="px-5 pt-28"><PageLoading /></div>}>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<OverviewPage />} />
          <Route path="runs" element={<RunsPage />} />
          <Route path="runs/:caseId/:runId" element={<RunDetailPage />} />
          <Route path="compare" element={<ComparePage />} />
          <Route path="cases" element={<CasesPage />} />
          <Route path="command-builder" element={<CommandBuilderPage />} />
          <Route path="workflow" element={<WorkflowPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </Suspense>
    {!consoleRoute&&<CompareTray />}
    <Toaster />
  </>;
}
