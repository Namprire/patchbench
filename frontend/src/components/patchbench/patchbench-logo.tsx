import { cn } from "../../lib/utils";
export function PatchBenchLogo({ className }: { className?: string }) { return <div aria-hidden="true" className={cn("relative h-8 w-8 rounded-full border border-current", className)}><span className="absolute inset-[22%] rounded-full border border-current opacity-60" /><span className="absolute inset-[42%] rounded-full bg-current" /></div>; }
