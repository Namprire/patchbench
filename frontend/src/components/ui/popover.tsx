import * as PopoverPrimitive from "@radix-ui/react-popover";
import { cn } from "../../lib/utils";
export const Popover = PopoverPrimitive.Root; export const PopoverTrigger = PopoverPrimitive.Trigger;
export const PopoverContent = ({ className, align = "center", sideOffset = 6, ...props }: React.ComponentPropsWithoutRef<typeof PopoverPrimitive.Content>) => <PopoverPrimitive.Portal><PopoverPrimitive.Content align={align} sideOffset={sideOffset} className={cn("z-[70] w-72 rounded-none border border-edge-strong bg-surface p-4 text-ink shadow-overlay", className)} {...props} /></PopoverPrimitive.Portal>;
