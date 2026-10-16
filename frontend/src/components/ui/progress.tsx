import * as ProgressPrimitive from "@radix-ui/react-progress";
import { cn } from "../../lib/utils";
type ProgressProps = React.ComponentPropsWithoutRef<typeof ProgressPrimitive.Root> & { indicatorClassName?: string; warn?: boolean };
export const Progress = ({ className, value = 0, indicatorClassName, warn = false, ...props }: ProgressProps) => <ProgressPrimitive.Root className={cn("relative h-0.5 w-full overflow-hidden rounded-none bg-inset", className)} {...props}><ProgressPrimitive.Indicator className={cn("h-full bg-ink transition-transform duration-2 ease-std", warn && "bg-warn", indicatorClassName)} style={{ transform: `translateX(-${100 - (value ?? 0)}%)` }} /></ProgressPrimitive.Root>;
