import { Toaster as Sonner } from "sonner";
import { useResolvedTheme } from "../patchbench/theme-toggle";
export const Toaster = () => { const theme = useResolvedTheme(); return <Sonner theme={theme} offset={{bottom:"calc(var(--compare-tray-h, 0px) + 16px)"}} mobileOffset={{bottom:"calc(var(--compare-tray-h, 0px) + 8px)"}} toastOptions={{ className: "rounded-none border border-edge-strong border-l-2 bg-surface text-ink shadow-overlay" }} />; };
