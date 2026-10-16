// Public profile: default data is an explicitly invented, reviewed demo.
import { copyFileSync, existsSync, mkdirSync, readdirSync, rmSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const root = path.resolve(frontend, "..");
const output = path.join(frontend, "public/data");
if (process.env.PATCHBENCH_CASES_DIR) {
  const localPython = path.join(root, ".venv/bin/python");
  const python = existsSync(localPython) ? localPython : "python3";
  const result = spawnSync(python, ["-m", "patchbench.cli", "export-dashboard",
    "--cases-dir", path.resolve(process.env.PATCHBENCH_CASES_DIR),
    "--runs-dir", path.resolve(process.env.PATCHBENCH_RUNS_DIR ?? path.join(root, "runs")),
    "--output", output], { cwd: root, stdio: "inherit",
    env: { ...process.env, PYTHONPATH: path.join(root, "src") } });
  if (result.error || result.status !== 0) throw new Error("Local evidence export failed");
} else {
  if (process.env.PATCHBENCH_RUNS_DIR) throw new Error("Select CASES_DIR with RUNS_DIR");
  const demo = path.join(frontend, "demo-data");
  if (!readdirSync(demo).includes("dashboard.json")) throw new Error("Synthetic demo data missing");
  rmSync(output, { recursive: true, force: true });
  mkdirSync(path.dirname(output), { recursive: true });
  const mapping = {
    "manifest.json": "manifest.json", "dashboard.json": "dashboard.json",
    "case-demo-label.json": "cases/demo-label.json",
    "run-demo-gold.json": "runs/demo-label/demo-gold.json",
    "run-demo-dummy.json": "runs/demo-label/demo-dummy.json",
    "case-demo-config.json": "cases/demo-config.json",
    "case-demo-ranges.json": "cases/demo-ranges.json",
    "run-demo-config-gold.json": "runs/demo-config/demo-config-gold.json",
    "run-demo-config-dummy.json": "runs/demo-config/demo-config-dummy.json",
    "run-demo-ranges-gold.json": "runs/demo-ranges/demo-ranges-gold.json",
    "run-demo-ranges-dummy.json": "runs/demo-ranges/demo-ranges-dummy.json",
  };
  for (const [source, destination] of Object.entries(mapping)) {
    const target = path.join(output, destination);
    mkdirSync(path.dirname(target), { recursive: true });
    copyFileSync(path.join(demo, source), target);
  }
  console.log("Prepared invented synthetic demo data; no research snapshot is loaded.");
}
