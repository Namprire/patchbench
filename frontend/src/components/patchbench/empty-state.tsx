import { DatabaseZap } from "lucide-react";

export function EmptyState({ title = "No exported data", description = "Regenerate the dashboard export, then refresh this page.", variant = "plain", className = "" }: { title?: string; description?: string; variant?: "plain" | "terminal"; className?: string }) {
  if (variant === "terminal") return <div className={`dot-grid border border-edge bg-canvas px-5 py-12 text-center ${className}`}><span className="inline-flex h-5 items-center bg-invert px-2 t-label text-invert-foreground">{title}</span><span aria-hidden="true" className="motion-safe:animate-pulse">_</span><p className="mx-auto mt-4 max-w-prose t-body-sm text-ink-muted">{description}</p></div>;
  return <div className={`border border-dotted border-edge bg-surface px-5 py-6 text-center ${className}`}><DatabaseZap className="mx-auto h-5 w-5 text-ink-subtle" /><h3 className="mt-4 t-subtitle">{title}</h3><p className="mx-auto mt-2 max-w-prose t-body-sm text-ink-muted">{description}</p></div>;
}
