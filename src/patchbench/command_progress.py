"""TTY activity indicator for CLI commands with no dedicated run dashboard."""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Mapping, TextIO

from patchbench.catalog import safe_component
from patchbench.terminal_output import (
    _ERASE_LINE, _clip_ansi, _is_ci, _style, _supports_color,
    _supports_unicode, _terminal_width, activity_bar, format_duration,
)


class _LogStream:
    """Clear the animated line before writing real terminal output."""

    def __init__(self, progress: "CommandProgress", stream: TextIO) -> None:
        self.progress = progress
        self.stream = stream

    def write(self, text: str) -> int:
        with self.progress.lock:
            self.progress._clear()
            count = self.stream.write(text)
            if text:
                self.progress.line_open = not text.endswith("\n")
            return count

    def flush(self) -> None:
        with self.progress.lock:
            self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


class CommandProgress:
    """A moving bar indicates activity, never an invented completion percentage."""

    def __init__(self, command: str, *, enabled: bool = True,
                 stream: TextIO | None = None, environment: Mapping[str, str] | None = None,
                 clock=time.monotonic) -> None:
        self.stream = stream if stream is not None else sys.stderr
        env = environment if environment is not None else os.environ
        tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.enabled = enabled and tty and not _is_ci(env) and env.get("TERM", "").lower() != "dumb"
        self.color = self.enabled and _supports_color(self.stream, env, tty=tty)
        self.unicode = _supports_unicode(self.stream)
        self.command = command
        self.clock = clock
        self.started = clock()
        self.index = 0
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.rendered = False
        self.line_open = False
        self.result: int | None = None
        self.current_case: str | None = None
        self.case_index = 0
        self.case_total = 0
        self.log_stream = _LogStream(self, self.stream) if self.enabled else self.stream
        stdout_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        self.output_stream = _LogStream(self, sys.stdout) if self.enabled and stdout_tty else sys.stdout

    def __enter__(self) -> "CommandProgress":
        if self.enabled:
            self.tick()
            self.thread = threading.Thread(target=self._animate, name="patchbench-progress", daemon=True)
            self.thread.start()
        return self

    def _animate(self) -> None:
        while not self.stop.wait(0.2):
            self.tick()

    def update_case(self, candidate_id: str, index: int, total: int) -> None:
        """Display only a safe identifier, never candidate descriptions or prompts."""
        if not safe_component(candidate_id) or not candidate_id.isprintable() or not 1 <= index <= total:
            return
        with self.lock:
            self.current_case = candidate_id
            self.case_index = index
            self.case_total = total
            self.tick()

    def tick(self) -> None:
        with self.lock:
            if not self.enabled or self.stop.is_set() or self.line_open:
                return
            self.index += 1
            label = (f"{self.current_case} | case {self.case_index}/{self.case_total}"
                     if self.current_case else f"PatchBench {self.command}")
            text = (_style(activity_bar(self.index, unicode_enabled=self.unicode), "active", self.color)
                    + f" {label} | running | "
                    + format_duration(self.clock() - self.started))
            self.stream.write(_ERASE_LINE + _clip_ansi(text, _terminal_width(self.stream) - 1))
            self.stream.flush()
            self.rendered = True

    def _clear(self) -> None:
        if self.rendered:
            self.stream.write(_ERASE_LINE)
            self.rendered = False

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        if not self.enabled:
            return
        with self.lock:
            self._clear()
            if self.line_open:
                self.stream.write("\n")
            status = "interrupted" if exc_type is not None else ("completed" if self.result == 0 else "failed")
            style = "warning" if exc_type is not None else ("success" if self.result == 0 else "error")
            text = f"PatchBench {self.command} | {status} | {format_duration(self.clock() - self.started)}"
            self.stream.write(_style(text, style, self.color) + "\n")
            self.stream.flush()
