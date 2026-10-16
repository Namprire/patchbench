import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../../lib/utils";

const buttonVariants = cva("inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-none text-[13px] font-medium transition-[background-color,border-color,color,transform,box-shadow] duration-2 ease-std focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/25 focus-visible:ring-offset-1 focus-visible:ring-offset-canvas active:translate-y-px disabled:pointer-events-none disabled:opacity-45 disabled:active:translate-y-0", { variants: { variant: { default: "border border-ink bg-ink text-canvas hover:bg-ink-muted active:bg-ink-subtle", outline: "border border-edge bg-surface hover:border-edge-strong hover:bg-hover active:bg-pressed", ghost: "border border-transparent hover:bg-hover active:bg-pressed", secondary: "border border-transparent bg-secondary text-secondary-foreground hover:border-edge hover:bg-hover active:bg-pressed", accent: "border border-brand bg-brand-soft text-brand-text hover:bg-chosen", danger: "border border-danger text-danger hover:bg-danger-soft" }, size: { default: "h-11 px-4 md:h-8", sm: "h-11 rounded-sm px-3 t-label-xs md:h-8", lg: "h-11 px-5 md:h-10", icon: "h-11 w-11 md:h-8 md:w-8" } }, defaultVariants: { variant: "default", size: "default" } });

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {}
export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(({ className, variant, size, ...props }, ref) => <button ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props} />);
Button.displayName = "Button";
export { buttonVariants };
