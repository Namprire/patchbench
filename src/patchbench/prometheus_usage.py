"""Optional, non-authoritative LiteLLM counter measurements (host side only)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from patchbench.mcp import parse_literal_env_file


METRICS = (
    "litellm_input_tokens_metric_total",
    "litellm_output_tokens_metric_total",
    "litellm_total_tokens_metric_total",
)
PREFIX = "PATCHBENCH_PROMETHEUS_"


class TokenMonitor:
    def __init__(self, model: str | None):
        self.report = {
            "source": "litellm_prometheus",
            "scope": "api_key_and_model",
            "measurement_type": "key_level_estimate",
            "authoritative": False,
            "attribution": "May include other requests sharing the key/model and scrape or callback lag; Claude Code events remain authoritative.",
            "model": model,
            "status": "unavailable",
            "metrics": {name: {"delta": None, "status": "unavailable"} for name in METRICS},
        }
        self.before = {}
        self.ended = None
        self.finished = False

    def start(self, env_file: str | Path | None) -> None:
        try:
            url = os.environ[PREFIX + "URL"].rstrip("/")
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("invalid URL")
            self.url = url + "/api/v1/query"
            self.timeout = float(os.environ.get(PREFIX + "HTTP_TIMEOUT_SECONDS", "3"))
            self.wait = float(os.environ.get(PREFIX + "WAIT_SECONDS", "60"))
            if not all(math.isfinite(v) and v > 0 for v in (self.timeout, self.wait)):
                raise ValueError("invalid timeout")
            self.key_label = os.environ.get(PREFIX + "KEY_LABEL", "hashed_api_key")
            self.model_label = os.environ.get(PREFIX + "MODEL_LABEL", "model")
            if self.key_label == self.model_label or any(
                not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", label) or label == "__name__"
                for label in (self.key_label, self.model_label)
            ):
                raise ValueError("invalid labels")
            if not isinstance(self.report["model"], str) or not self.report["model"]:
                raise ValueError("explicit model required")
            self.key_hash = os.environ.get(PREFIX + "KEY_HASH")
            if self.key_hash is None:
                values = parse_literal_env_file(Path(env_file), label="Claude") if env_file is not None else os.environ
                key = values.get("ANTHROPIC_AUTH_TOKEN") or values.get("ANTHROPIC_API_KEY")
                if not key:
                    raise ValueError("credential unavailable")
                self.key_hash = hashlib.sha256(key.encode()).hexdigest()
            if not re.fullmatch(r"[0-9a-fA-F]{64}", self.key_hash):
                raise ValueError("hashed key required")
            self.bearer = os.environ.get(PREFIX + "BEARER_TOKEN")
        except Exception:
            self.report["reason"] = "invalid_configuration"
            return
        try:
            self.before = self._query(300)
            if not self.before:
                self.report["reason"] = "missing_baseline"
        except Exception:
            # Never persist exception text, query strings, labels or HTTP bodies.
            self.report["reason"] = "baseline_request_failed"
        self.report["execution_started_at"] = time.time()

    def stop(self) -> None:
        if self.ended is None:
            self.ended = time.time()
            self.report["execution_finished_at"] = self.ended

    def _query(self, window: float) -> dict:
        selector = (
            '{__name__=~' + json.dumps("|".join(METRICS)) + ','
            + self.key_label + '=' + json.dumps(self.key_hash) + ','
            + self.model_label + '=' + json.dumps(self.report["model"]) + '}'
        )
        # Range-vector sample timestamps are actual scrape timestamps. Instant
        # vector timestamps would describe query evaluation, even for old data.
        data = urllib.parse.urlencode({"query": selector + f"[{math.ceil(window)}s]"}).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.bearer:
            headers["Authorization"] = "Bearer " + self.bearer
        request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.load(response)
        if payload.get("status") != "success" or payload.get("warnings") or payload.get("data", {}).get("resultType") != "matrix":
            raise ValueError("invalid or incomplete response")
        result = {}
        for series in payload["data"]["result"]:
            labels = series["metric"]
            if labels.get("__name__") not in METRICS or labels.get(self.key_label) != self.key_hash or labels.get(self.model_label) != self.report["model"]:
                raise ValueError("unexpected series")
            identity = tuple(sorted(labels.items()))
            samples = [(float(t), float(v)) for t, v in series["values"]]
            if not samples:
                continue
            if identity in result or any(not math.isfinite(t) or not math.isfinite(v) or v < 0 for t, v in samples):
                raise ValueError("invalid samples")
            result[identity] = sorted(samples)
        return result

    def _measure(self, after: dict) -> tuple[dict, bool]:
        metrics = {}
        ready = True
        for name in METRICS:
            before = {k: v[-1] for k, v in self.before.items() if dict(k)["__name__"] == name}
            current = {k: v for k, v in after.items() if dict(k)["__name__"] == name}
            item = {"delta": None, "status": "missing_before"}
            metrics[name] = item
            if not before:
                continue
            item["before"] = sum(v for _, v in before.values())
            item["before_sample_time"] = min(t for t, _ in before.values())
            if current.keys() - before.keys():
                item["status"] = "series_changed"
                continue
            fresh = {k: next(((t, v) for t, v in current.get(k, []) if t > self.ended), None) for k in before}
            if any(v is None for v in fresh.values()):
                item["status"] = "awaiting_scrape"
                ready = False
                continue
            item["after"] = sum(v for _, v in fresh.values())
            item["after_sample_time"] = min(t for t, _ in fresh.values())
            reset = False
            for key, (start, value) in before.items():
                history = [value] + [v for t, v in current[key] if start < t <= fresh[key][0]]
                reset |= any(b < a for a, b in zip(history, history[1:]))
            if reset:
                item["status"] = "counter_reset"
            else:
                item.update(status="ok", delta=item["after"] - item["before"])
        return metrics, ready

    def finish(self) -> dict:
        if self.finished:
            return self.report
        self.finished = True
        if self.report.get("reason"):
            return self.report
        if self.ended is None:
            self.report["reason"] = "execution_end_missing"
            return self.report
        try:
            oldest = min(samples[-1][0] for samples in self.before.values())
            deadline = time.monotonic() + max(0, self.wait - (time.time() - self.ended))
            while True:
                metrics, ready = self._measure(self._query(max(300, time.time() - oldest + 5)))
                if ready or time.monotonic() >= deadline:
                    break
                time.sleep(min(1, max(0, deadline - time.monotonic())))
            for item in metrics.values():
                if item["status"] == "awaiting_scrape":
                    item["status"] = "scrape_timeout"
            self.report["metrics"] = metrics
            count = sum(item["status"] == "ok" for item in metrics.values())
            self.report["status"] = "ok" if count == len(METRICS) else "partial" if count else "unavailable"
        except Exception:
            self.report["reason"] = "collection_failed"
        return self.report


def begin_monitoring(env_file: str | Path | None, model: str | None) -> TokenMonitor | None:
    if not os.environ.get(PREFIX + "URL"):
        return None
    monitor = TokenMonitor(model)
    monitor.start(env_file)
    return monitor
