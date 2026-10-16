import { Moon, Sun } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Button } from "../ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";

export type ThemeMode = "light" | "dark";
export type ResolvedTheme = "light" | "dark";

const THEME_KEY = "patchbench.theme";
const THEME_EVENT = "patchbench-theme-change";
const mediaQuery = "(prefers-color-scheme: dark)";

function storedTheme(): ThemeMode {
  if (typeof window === "undefined") return "light";
  const value = window.localStorage.getItem(THEME_KEY);
  if (value === "light" || value === "dark") return value;
  return window.matchMedia(mediaQuery).matches ? "dark" : "light";
}

function stampTheme(mode: ThemeMode) {
  document.documentElement.classList.toggle("dark", mode === "dark");
  document.documentElement.style.colorScheme = mode;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content",mode==="dark"?"#0f0f0f":"#f3f1ec");
  window.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: { mode, resolved: mode } }));
  return mode;
}

export function useResolvedTheme(): ResolvedTheme {
  const [resolved, setResolved] = useState<ResolvedTheme>(() =>
    typeof window === "undefined" ? "light" : storedTheme(),
  );
  useEffect(() => {
    const update = () => setResolved(stampTheme(storedTheme()));
    const onTheme = (event: Event) => setResolved((event as CustomEvent<{ resolved: ResolvedTheme }>).detail?.resolved ?? storedTheme());
    update();
    window.addEventListener(THEME_EVENT, onTheme);
    window.addEventListener("storage", update);
    return () => {
      window.removeEventListener(THEME_EVENT, onTheme);
      window.removeEventListener("storage", update);
    };
  }, []);
  return resolved;
}

export function ThemeToggle() {
  const [mode, setMode] = useState<ThemeMode>(storedTheme);
  const choose = useCallback((value: ThemeMode) => {
    setMode(value);
    window.localStorage.setItem(THEME_KEY, value);
    stampTheme(value);
  }, []);
  useEffect(() => {
    stampTheme(mode);
  }, [mode]);
  const next: ThemeMode = mode === "dark" ? "light" : "dark";
  const label = `Switch to ${next} mode`;
  return <Tooltip><TooltipTrigger asChild><Button type="button" variant="ghost" size="icon" className="relative overflow-hidden text-ink-muted hover:text-ink" aria-label={label} onClick={() => choose(next)}>
    <Moon aria-hidden="true" className={`absolute h-4 w-4 transition-all duration-3 ${mode==="light"?"rotate-0 scale-100 opacity-100":"-rotate-90 scale-50 opacity-0"}`} />
    <Sun aria-hidden="true" className={`absolute h-4 w-4 transition-all duration-3 ${mode==="dark"?"rotate-0 scale-100 opacity-100":"rotate-90 scale-50 opacity-0"}`} />
  </Button></TooltipTrigger><TooltipContent>{label}</TooltipContent></Tooltip>;
}
