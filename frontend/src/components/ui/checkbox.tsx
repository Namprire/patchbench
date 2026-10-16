import * as CheckboxPrimitive from "@radix-ui/react-checkbox";
import { Check } from "lucide-react";
import { cn } from "../../lib/utils";
export const Checkbox = ({ className, ...props }: React.ComponentPropsWithoutRef<typeof CheckboxPrimitive.Root>) => <CheckboxPrimitive.Root className={cn("peer h-4 w-4 shrink-0 rounded-sm border border-edge bg-surface text-canvas transition-[background-color,border-color,box-shadow] duration-2 hover:border-edge-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/25 data-[state=checked]:border-brand data-[state=checked]:bg-brand", className)} {...props}><CheckboxPrimitive.Indicator className="flex items-center justify-center"><Check className="h-3.5 w-3.5" /></CheckboxPrimitive.Indicator></CheckboxPrimitive.Root>;
