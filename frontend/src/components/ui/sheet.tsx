import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "../../lib/utils";
export const Sheet = DialogPrimitive.Root; export const SheetTrigger = DialogPrimitive.Trigger; export const SheetClose = DialogPrimitive.Close;
export const SheetContent = ({ className, children, ...props }: React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content>) => <DialogPrimitive.Portal><DialogPrimitive.Overlay className="fixed inset-0 z-[80] bg-canvas/80" /><DialogPrimitive.Content className={cn("fixed inset-y-0 right-0 z-[90] w-[90vw] max-w-[480px] rounded-none border-l border-edge-strong bg-surface p-6 text-ink shadow-overlay", className)} {...props}>{children}<DialogPrimitive.Close className="absolute right-2 top-2 z-20 flex h-11 w-11 items-center justify-center rounded-none hover:bg-hover md:right-4 md:top-4 md:h-7 md:w-7"><X className="h-4 w-4" /><span className="sr-only">Close</span></DialogPrimitive.Close></DialogPrimitive.Content></DialogPrimitive.Portal>;
export const SheetHeader = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => <div className={cn("mb-6 space-y-1", className)} {...props} />;
export const SheetTitle = DialogPrimitive.Title; export const SheetDescription = DialogPrimitive.Description;
