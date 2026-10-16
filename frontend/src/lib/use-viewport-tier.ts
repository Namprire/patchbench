import { useEffect, useState } from "react";

export type ViewportTier = "xs" | "sm" | "md" | "lg" | "xl";

function currentTier(): ViewportTier {
  const width = typeof window === "undefined" ? 1280 : window.innerWidth;
  if (width < 640) return "xs";
  if (width < 768) return "sm";
  if (width < 1024) return "md";
  if (width < 1280) return "lg";
  return "xl";
}

export function useViewportTier() {
  const [tier, setTier] = useState<ViewportTier>(currentTier);
  useEffect(() => {
    const update = () => setTier(currentTier());
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);
  return tier;
}
