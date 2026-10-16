import { Link } from "react-router-dom";
import { buttonVariants } from "../components/ui/button";

export function NotFoundPage(){return <div className="flex min-h-[calc(100vh-var(--shell-offset)-64px)] items-center justify-center border border-edge-subtle px-5 py-16 text-center"><div className="max-w-prose border-l-2 border-l-brand pl-6 text-left"><div className="inline-flex bg-invert px-3 py-2 t-label text-invert-foreground">404<span className="motion-safe:animate-pulse">_</span></div><h1 className="mt-5 t-title">Page not found</h1><p className="mt-3 t-body text-ink-muted">The requested route does not exist. Return to the exported evidence workspace to continue your review.</p><Link className={`${buttonVariants({variant:"outline"})} mt-6`} to="/">Return to overview</Link></div></div>}
