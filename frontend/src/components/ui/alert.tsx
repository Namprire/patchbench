import { cn } from "../../lib/utils";
type AlertVariant = "strong" | "warn" | "danger";
export function Alert({ className, variant = "strong", ...props }: React.HTMLAttributes<HTMLDivElement> & { variant?: AlertVariant }) { return <div role="alert" className={cn("relative w-full rounded-none border border-edge bg-surface px-3 py-3 t-body-sm", variant === "strong" && "border-l-2 border-l-edge-strong", variant === "warn" && "border-l-2 border-l-warn", variant === "danger" && "border-l-2 border-l-danger", className)} {...props} />; }
export function AlertTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) { return <h5 className={cn("mb-1 t-subtitle", className)} {...props} />; }
export function AlertDescription({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) { return <div className={cn("t-body-sm text-ink-muted", className)} {...props} />; }
