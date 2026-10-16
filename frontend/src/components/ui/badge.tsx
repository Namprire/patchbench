import { cva, type VariantProps } from "class-variance-authority";
import type * as React from "react";
import { cn } from "../../lib/utils";
const badgeVariants = cva("inline-flex h-5 items-center gap-1 whitespace-nowrap rounded-sm border px-1.5 t-label-xs", { variants: { variant: {
  solid: "border-edge-strong bg-surface text-ink",
  warn: "border-dashed border-warn bg-warn-soft text-warn",
  danger: "border-danger bg-danger-soft text-danger",
  accent: "border-brand-border bg-brand-soft text-brand-text",
  quiet: "border-edge bg-surface text-ink-muted",
  absent: "border-dotted border-edge bg-transparent text-ink-subtle",
  default: "border-edge-strong bg-surface text-ink",
  outline: "border-edge bg-surface text-ink-muted",
  muted: "border-edge bg-surface text-ink-muted",
  dashed: "border-dotted border-edge bg-transparent text-ink-subtle",
} }, defaultVariants: { variant: "outline" } });
export interface BadgeProps extends React.HTMLAttributes<HTMLDivElement>, VariantProps<typeof badgeVariants> {}
export function Badge({ className, variant, ...props }: BadgeProps) { return <div className={cn(badgeVariants({ variant }), className)} {...props} />; }
