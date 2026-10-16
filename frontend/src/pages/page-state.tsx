import { AlertTriangle, Copy } from "lucide-react";
import { toast } from "sonner";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Skeleton } from "../components/ui/skeleton";

const exportCommand = "patchbench export-dashboard --cases-dir cases --runs-dir runs --output frontend/public/data";
export function PageLoading() { return <div className="space-y-5"><Skeleton className="h-8 w-2/3" /><Skeleton className="h-24 w-full" /><Skeleton className="h-80 w-full" /></div>; }
export function PageError({ message }: { message: string }) { return <Alert variant="danger"><AlertTriangle className="mb-2 h-4 w-4 text-danger" /><AlertTitle>The exported run data could not be loaded.</AlertTitle><AlertDescription>{message}<div className="mt-3 flex items-start border border-edge-subtle bg-inset"><pre className="min-w-0 flex-1 overflow-x-auto p-2 t-console text-ink">{exportCommand}</pre><Button aria-label="Copy export command" variant="ghost" size="icon" onClick={() => { void navigator.clipboard.writeText(exportCommand); toast.success("Export command copied"); }}><Copy className="h-3.5 w-3.5" /></Button></div></AlertDescription></Alert>; }
export function PageHeading({ eyebrow, title, description }: { eyebrow: string; title: string; description?: string }) { return <header className="mb-8 max-w-prose pt-7"><p className="flex items-center gap-2 t-label text-ink-subtle before:h-px before:w-5 before:bg-brand">{eyebrow}</p><h1 className="mt-3 t-display">{title}</h1>{description && <p className="mt-2 max-w-prose t-body text-ink-muted">{description}</p>}</header>; }
