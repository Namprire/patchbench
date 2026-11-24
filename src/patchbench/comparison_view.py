"""One terminal owner for the independent xargs comparison workers."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import time

from patchbench.terminal_output import RunDisplay, _PHASE_LABELS


class ComparisonDisplay(RunDisplay):
    def __init__(self, directory: Path, **kwargs):
        super().__init__(case_id="Model comparison", run_id=directory.name,
                         agent="claude-code", max_turns=None, live=True, **kwargs)
        self.directory = directory
        self.jobs = []
        for row in (directory / "jobs.tsv").read_text(encoding="utf-8").splitlines():
            index, model = row.split("\t")
            name = f"{index}-{model.rsplit('/', 1)[-1]}"
            card = RunDisplay(case_id="Preparing", run_id=name, agent=model,
                              max_turns=None, live=True, stream=self.stream)
            card.color_enabled = self.color_enabled
            self.jobs.append((name, model, card))
        self.reported = set()

    def render(self) -> None:
        active, finished, waiting = [], [], 0
        for name, model, card in self.jobs:
            try:
                state = json.loads((self.directory / f"{name}.progress.json").read_text(encoding="utf-8"))
                if state.get("phase") in _PHASE_LABELS:
                    for key in ("case_id", "run_id", "max_turns", "phase", "phase_status",
                                "container_status", "turns", "tool_calls", "last_activity",
                                "tests_status", "evaluation_status"):
                        if key in state:
                            setattr(card, key, state[key])
                    elapsed = state["elapsed_seconds"]
                    if not state.get("closed"):
                        elapsed += max(0, time.time() - state["updated_at"])
                    card.started = card.clock() - elapsed
            except (OSError, ValueError, KeyError, TypeError):
                pass  # Missing initial snapshot or a transient read must not stop jobs.
            card.agent = f"claude-code / {model}"
            card.terminal_width = self.terminal_width
            card._spinner_index += 1
            try:
                code = (self.directory / f"{name}.exit").read_text().strip()
            except OSError:
                code = ""
            if code:
                outcome = "Successful" if code == "0" else "Interrupted" if code in {"130", "143"} else f"Failed (exit {code})"
                finished.append(f"{model}: {outcome} · Tests: {card.tests_status}")
                if not self.dynamic and name not in self.reported:
                    self.stream.write(finished[-1] + "\n")
                    self.reported.add(name)
            elif (self.directory / f"{name}.pid").exists():
                active.append((model, card))
                if not self.dynamic:
                    card._render_line(heartbeat=False)
            else:
                waiting += 1
        if not self.dynamic:
            self.stream.flush()
            return
        header = ["PatchBench · Model comparison",
                  f"Running: {len(active)} · Completed: {len(finished)}/{len(self.jobs)} · Waiting: {waiting}"]
        blocks = [(model, card, card._dynamic_lines()) for model, card in active]
        try:
            height = os.get_terminal_size(self.stream.fileno()).lines or 40
        except (OSError, ValueError, AttributeError):
            height = 40
        lines = list(header)
        full_height = len(header) + sum(len(block) + 1 for _, _, block in blocks) + len(finished)
        for model, card, block in blocks:
            if full_height < height:
                lines.extend(block + [""])
            else:
                # Reuse the same fields, omitting repeated identity/separator rows.
                lines.append(f"{model} · {card.case_id}")
                if len(header) + 8 * len(blocks) + len(finished) < height:
                    lines.extend(block[index] for index in (4, 5, 7, 8, 9, 10, 11))
                else:
                    lines.extend((block[5] + " · " + block[4], block[7] + " · " + block[8],
                                  block[9], block[10] + " · " + block[11]))
        lines.extend(finished)
        if len(lines) >= height:
            # Cursor-relative redraws must fit on screen, including small terminals.
            lines = lines[:max(1, height - 2)] + ["More details in the per-model logs."]
        self._paint_lines(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    # Workers handle terminal Ctrl+C. Keep drawing until the launcher has waited
    # for their cleanup and publishes .done, then restore the terminal cursor.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    display = ComparisonDisplay(args.directory)
    try:
        while True:
            display.render()
            if (args.directory / ".done").exists():
                break
            time.sleep(0.2)
    finally:
        display.close()


if __name__ == "__main__":
    main()
