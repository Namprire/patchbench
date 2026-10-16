import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatNumber(value: number | null | undefined): string {
  return value == null ? "—" : new Intl.NumberFormat("en-US").format(value);
}

export function formatCompactNumber(value: number | null | undefined): string {
  if (value == null) return "—";
  return new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

export function formatDuration(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value < 60) return `${value.toFixed(1)}s`;
  return `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`;
}

export function formatCost(value: number | null | undefined): string {
  return value == null ? "—" : `$${value.toFixed(value < 1 ? 3 : 2)}`;
}

export function compactLabel(value: string | null | undefined): string {
  return value?.replaceAll("_", " ") ?? "unknown";
}
