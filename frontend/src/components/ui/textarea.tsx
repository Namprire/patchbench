import * as React from "react";
import { cn } from "../../lib/utils";

export const Textarea=React.forwardRef<HTMLTextAreaElement,React.TextareaHTMLAttributes<HTMLTextAreaElement>>(({className,...props},ref)=><textarea ref={ref} className={cn("min-h-28 w-full resize-y rounded-none border border-edge bg-surface px-2.5 py-2 t-mono-sm text-ink transition-[background-color,border-color,box-shadow] placeholder:text-ink-subtle hover:border-edge-strong focus-visible:border-brand focus-visible:bg-canvas focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/15 disabled:bg-inset disabled:opacity-45",className)} {...props}/>);
Textarea.displayName="Textarea";
