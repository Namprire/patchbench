export function MetricCluster({ title, children, columns = 4, className = "" }: { title: string; children: React.ReactNode; columns?: 2 | 4; className?: string }) {
  return <section className={`border border-edge bg-surface ${className}`}>
    <h3 className="flex h-9 items-center border-b border-edge-subtle bg-inset px-3 t-label text-ink-subtle">{title}</h3>
    <div className={`grid gap-px bg-edge-subtle sm:grid-cols-2 ${columns === 4 ? "md:grid-cols-4" : ""}`}>{children}</div>
  </section>;
}
