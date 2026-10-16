import { Menu, RefreshCw } from "lucide-react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { toast } from "sonner";
import { useDashboard } from "../../data/dashboard";
import { cn } from "../../lib/utils";
import { Alert, AlertDescription } from "../ui/alert";
import { Button } from "../ui/button";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "../ui/sheet";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "../ui/tooltip";
import { DataFreshness } from "./data-freshness";
import { PatchBenchLogo } from "./patchbench-logo";
import { ThemeToggle } from "./theme-toggle";

const links = [["/", "Overview"], ["/runs", "Runs"], ["/workflow", "Build"], ["/compare", "Compare"], ["/cases", "Cases"]] as const;
const navClass = ({ isActive }: { isActive: boolean }) => cn(
  "relative flex h-12 items-center px-3 t-label text-ink-muted transition-colors duration-2 hover:bg-hover hover:text-ink focus-visible:outline-none after:absolute after:inset-x-3 after:bottom-0 after:h-0.5 after:bg-transparent",
  isActive && "bg-chosen text-ink after:bg-brand",
);

export function AppShell() {
  const { pathname } = useLocation();
  const linkActive=(to:string)=>to==="/"?pathname==="/":pathname===to||pathname.startsWith(`${to}/`);
  const { manifest, dashboard, loading, error, refreshing, refreshError, refresh } = useDashboard();
  const repositories = [...new Set(dashboard?.cases.map((item) => item.repository).filter((item): item is string => Boolean(item)) ?? [])];
  const repositoryScope = repositories.length === 1 ? repositories[0].toUpperCase() : repositories.length > 1 ? "ALL REPOSITORIES" : "UNKNOWN";
  const stale = manifest?.generated_at ? Date.now() - new Date(manifest.generated_at).getTime() > 24 * 60 * 60 * 1000 : false;
  const incomplete = Boolean(dashboard && (dashboard.run_count !== dashboard.runs.length || dashboard.case_count !== dashboard.cases.length));
  const syntheticExamples = Boolean(dashboard?.cases.length && dashboard.cases.every((item) => item.issue_title.startsWith("Synthetic demo:")));
  const handleRefresh = async () => { try { await refresh(); toast.success("Dashboard data refreshed"); } catch { toast.error("Dashboard refresh failed"); } };
  const freshness = { timestamp: manifest?.generated_at ?? null, schemaVersion: manifest?.schema_version ?? null, repositoryScope, caseCount: dashboard?.case_count ?? null, runCount: dashboard?.run_count ?? null, loading };

  return <TooltipProvider>
    <header className="app-chrome fixed inset-x-0 top-0 z-50 bg-surface">
      <div className="flex h-12 items-center border-b border-edge-strong px-4 md:px-6 xl:px-8">
        <NavLink to="/" className="-ml-4 flex h-12 shrink-0 items-center gap-2 bg-invert px-4 text-invert-foreground transition-opacity hover:opacity-90 md:-ml-6 xl:-ml-8">
          <PatchBenchLogo className="h-3.5 w-3.5" />
          <span className="t-label">PatchBench</span>
        </NavLink>
        <nav aria-label="Primary navigation" className="ml-3 hidden min-w-0 items-center lg:flex">{links.map(([to, label]) => {const active=linkActive(to);return <Link key={to} to={to} className={navClass({isActive:active})} aria-current={active?"page":undefined}>{label}</Link>})}</nav>
        <div className="ml-auto flex shrink-0 items-center gap-0.5 border-l border-edge-subtle pl-2">
          <ThemeToggle />
          <Tooltip><TooltipTrigger asChild><Button aria-label="Refresh exported data" variant="ghost" size="icon" disabled={refreshing} onClick={() => void handleRefresh()}><RefreshCw className={cn("h-4 w-4", refreshing && "motion-safe:animate-spin")} /></Button></TooltipTrigger><TooltipContent>{refreshing ? "Refreshing…" : "Refresh"}</TooltipContent></Tooltip>
          <Sheet><SheetTrigger asChild><Button aria-label="Open navigation" variant="ghost" size="icon" className="lg:hidden"><Menu className="h-5 w-5" /></Button></SheetTrigger><SheetContent className="flex w-[min(320px,88vw)] flex-col p-0">
            <SheetHeader className="mb-0 border-b border-edge-strong pr-12"><SheetTitle className="flex h-12 items-center gap-2 bg-invert px-4 text-invert-foreground"><PatchBenchLogo className="h-3.5 w-3.5" /><span className="t-label">PatchBench</span></SheetTitle><p className="px-4 pb-3 t-label-xs text-ink-subtle">Evaluation workspace</p></SheetHeader>
            <nav className="flex flex-col">{links.map(([to, label]) => {const active=linkActive(to);return <Link key={to} to={to} aria-current={active?"page":undefined} className={cn("flex h-12 items-center border-b border-edge-subtle px-4 t-label text-ink-muted", active && "border-l-2 border-l-brand text-ink")}>{label}</Link>})}</nav>
            <div className="mt-auto space-y-4 border-t border-edge-strong p-4">
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 t-mono-xs"><dt className="text-ink-subtle">SCHEMA</dt><dd>v{manifest?.schema_version ?? "?"}</dd><dt className="text-ink-subtle">CASES / RUNS</dt><dd>{dashboard?.case_count ?? 0} / {dashboard?.run_count ?? 0}</dd><dt className="text-ink-subtle">SCOPE</dt><dd>{repositoryScope}</dd><dt className="text-ink-subtle">MODE</dt><dd className="text-brand-text">READ-ONLY DEFAULT</dd></dl>
            </div>
          </SheetContent></Sheet>
        </div>
      </div>
      <div className={cn("h-7 border-b border-edge-strong bg-canvas", refreshing && "opacity-60")}><DataFreshness {...freshness} /></div>
    </header>
    <main className={cn("min-h-screen px-4 pt-[var(--shell-offset)] md:px-6 xl:px-8",pathname==="/workflow"||/^\/runs\/[^/]+\/[^/]+$/.test(pathname)?"":"pb-16")} style={pathname==="/workflow"||/^\/runs\/[^/]+\/[^/]+$/.test(pathname)?{paddingBottom:"calc(var(--console-h, 0px) + var(--console-page-gap))"}:{paddingBottom:"calc(var(--compare-tray-h, 0px) + 64px)"}}>
      <div className="mx-auto max-w-page pt-4">
        <div className="space-y-2">{syntheticExamples && <Alert><AlertDescription>Synthetic examples: these outcomes demonstrate the workflow and are not research benchmark results.</AlertDescription></Alert>}{refreshError && <Alert variant="danger"><AlertDescription>Refresh failed. The previous export remains available. {refreshError}</AlertDescription></Alert>}{stale && <Alert variant="warn"><AlertDescription>The local export is more than 24 hours old. Last export: <span className="font-mono text-xs">{new Date(manifest!.generated_at).toLocaleString()}</span></AlertDescription></Alert>}{!loading && !error && !dashboard && <Alert variant="danger"><AlertDescription>The dashboard export does not contain displayable data. Regenerate the local export and refresh.</AlertDescription></Alert>}{incomplete && <Alert variant="warn"><AlertDescription>This export is incomplete: the manifest counts do not match the available case or run summaries. Historical records remain visible.</AlertDescription></Alert>}</div>
        <Outlet />
      </div>
    </main>
  </TooltipProvider>;
}
