import { Command as CommandPrimitive } from "cmdk";
import { Search } from "lucide-react";
import { cn } from "../../lib/utils";
export const Command = ({ className, ...props }: React.ComponentPropsWithoutRef<typeof CommandPrimitive>) => <CommandPrimitive className={cn("flex h-full w-full flex-col overflow-hidden rounded-none bg-surface text-ink", className)} {...props} />;
export const CommandInput = ({ className, ...props }: React.ComponentPropsWithoutRef<typeof CommandPrimitive.Input>) => <div className="flex items-center border-b border-edge-subtle px-3"><Search className="mr-2 h-3.5 w-3.5 text-ink-subtle" /><CommandPrimitive.Input className={cn("h-11 w-full bg-transparent t-body-sm outline-none placeholder:text-ink-subtle md:h-8", className)} {...props} /></div>;
export const CommandList = ({ className, ...props }: React.ComponentPropsWithoutRef<typeof CommandPrimitive.List>) => <CommandPrimitive.List className={cn("max-h-64 overflow-y-auto p-1", className)} {...props} />;
export const CommandEmpty = ({ className, ...props }: React.ComponentPropsWithoutRef<typeof CommandPrimitive.Empty>) => <CommandPrimitive.Empty className={cn("h-8 py-2 text-center t-body-sm text-ink-muted", className)} {...props} />;
export const CommandGroup = ({ className, ...props }: React.ComponentPropsWithoutRef<typeof CommandPrimitive.Group>) => <CommandPrimitive.Group className={cn("overflow-hidden text-foreground", className)} {...props} />;
export const CommandItem = ({ className, ...props }: React.ComponentPropsWithoutRef<typeof CommandPrimitive.Item>) => <CommandPrimitive.Item className={cn("flex h-11 cursor-default select-none items-center rounded-none px-2 t-body-sm outline-none data-[selected=true]:bg-hover md:h-7", className)} {...props} />;
