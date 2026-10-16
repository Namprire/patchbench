"""Deterministic answer scoring driven by declared assertions and evaluator data."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import quote

from patchbench.exceptions import PatchbenchError

def score_answer(
    task: Any,
    final_answer: str,
    *,
    expected_answer_sha256: str | None = None,
) -> tuple[bool, bool, tuple[dict[str, Any], ...]]:
    parsed = _parse_answer_json(final_answer)
    answer = parsed.get("answer") if isinstance(parsed, dict) else None
    normalized_answer = _normalize_answer(task, answer)
    format_correct = _is_strict_json_answer(final_answer)
    if expected_answer_sha256 is not None:
        answer_correct = isinstance(answer, dict) and _answer_sha256(answer) == expected_answer_sha256
        return (
            answer_correct,
            format_correct,
            (
                {
                    "path": "$",
                    "observed": "answer_sha256" if isinstance(answer, dict) else None,
                    "passed": answer_correct,
                },
            ),
        )
    outcomes: list[dict[str, Any]] = []
    for assertion in task.answer_assertions:
        found, observed = _json_path(normalized_answer, assertion.path)
        passed = found and observed == assertion.equals
        outcomes.append(
            {
                "path": assertion.path,
                "observed": observed if found else None,
                "passed": passed,
            }
        )
    return (
        bool(outcomes) and all(item["passed"] for item in outcomes),
        format_correct,
        tuple(outcomes),
    )


def _parse_answer_json(value: str) -> Any:
    stripped = value.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    fences = re.findall(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL | re.IGNORECASE)
    for candidate in reversed(fences):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _is_strict_json_answer(value: str) -> bool:
    stripped = value.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        stripped,
        re.DOTALL | re.IGNORECASE,
    )
    if fenced is not None:
        stripped = fenced.group(1)
    try:
        return isinstance(json.loads(stripped), dict)
    except json.JSONDecodeError:
        return False


def _answer_sha256(answer: dict[str, Any]) -> str:
    canonical = json.dumps(answer, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_path(value: Any, path: str) -> tuple[bool, Any]:
    if path == "$":
        return True, value
    current = value
    for segment in path.removeprefix("$.").split("."):
        match = re.fullmatch(r"([^\[]+)(?:\[(\d+)\])?", segment)
        if match is None or not isinstance(current, dict) or match.group(1) not in current:
            return False, None
        current = current[match.group(1)]
        if match.group(2) is not None:
            index = int(match.group(2))
            if not isinstance(current, list) or index >= len(current):
                return False, None
            current = current[index]
    return True, current


def _observed_mcp_calls(events: tuple[dict[str, Any], ...], policy: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    prefix = f"mcp__{policy['mcp_server']}__"
    calls: list[dict[str, Any]] = []
    for event in events:
        content = event.get("content")
        message = event.get("message")
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if isinstance(name, str) and name.startswith(prefix):
                raw_input = block.get("input")
                calls.append(
                    {
                        "name": name.removeprefix(prefix),
                        "input": dict(raw_input) if isinstance(raw_input, dict) else {},
                    }
                )
    return tuple(calls)


def _score_tools(
    task: Any, condition: str, calls: tuple[dict[str, Any], ...]
) -> bool:
    tools = tuple(str(call["name"]) for call in calls)
    if condition == "none":
        return not tools
    if not tools:
        return False
    if any(_looks_like_write_tool(tool, task.policy) for tool in tools):
        return False
    if condition == "curated":
        return task.expected_curated_tool in tools
    allowed = set(task.policy["discovery_tools"]) | {task.policy["read_tool"]}
    if any(tool not in allowed for tool in tools):
        return False
    discovery_observed = any(
        tool in task.policy["discovery_tools"]
        for tool in tools
    )
    read_calls = [call for call in calls if call["name"] == task.policy["read_tool"]]
    return discovery_observed and any(
        call["input"].get(task.policy["operation_argument"]) == task.expected_operation_id
        for call in read_calls
    )


def _looks_like_write_tool(tool: str, policy: dict[str, Any]) -> bool:
    return any(marker in tool for marker in policy["forbidden_tool_markers"])


def _mcp_call_counts(
    task: Any,
    condition: str,
    calls: tuple[dict[str, Any], ...],
) -> tuple[int, int, int]:
    if condition == "curated":
        matching = sum(call["name"] == task.expected_curated_tool for call in calls)
    elif condition == "broad":
        matching = sum(
            call["name"] == task.policy["read_tool"]
            and call["input"].get(task.policy["operation_argument"]) == task.expected_operation_id
            for call in calls
        )
    else:
        matching = 0
    discovery = sum(
        call["name"] in task.policy["discovery_tools"]
        for call in calls
    )
    if condition == "curated":
        required_call_count = min(matching, 1)
    elif condition == "broad":
        discovery_kinds = {
            call["name"]
            for call in calls
            if call["name"]
            in task.policy["discovery_tools"]
        }
        required_call_count = len(discovery_kinds) + min(matching, 1)
    else:
        required_call_count = 0
    redundant = max(0, len(calls) - required_call_count)
    return matching, discovery, redundant


def _live_operation_selected(
    task: Any, condition: str, calls: tuple[dict[str, Any], ...]
) -> bool:
    if condition == "none":
        return True
    if condition == "curated":
        return any(call["name"] == task.expected_curated_tool for call in calls)
    return any(
        call["name"] == task.policy["read_tool"]
        and call["input"].get(task.policy["operation_argument"]) == task.expected_operation_id
        for call in calls
    )


def _render_path(template: str, parameters: dict[str, Any]) -> str:
    value = template
    for key, item in parameters.items():
        value = value.replace("{" + key + "}", quote(str(item), safe=""))
    return value


def _score_http(
    task: Any, condition: str, requests: tuple[dict[str, Any], ...]
) -> bool:
    if condition == "none":
        return not requests
    if not requests:
        return False
    return _matching_http_request_count(task, requests) > 0


def _matching_http_request_count(
    task: Any, requests: tuple[dict[str, Any], ...]
) -> int:
    expected_query = {key: [str(value)] for key, value in task.expected_query_parameters.items()}
    expected_path_parameters = {
        key: str(value) for key, value in task.expected_path_parameters.items()
    }
    expected_status = task.evaluation["expected_http_status"]
    return sum(
        request.get("method") == task.expected_http_method
        and request.get("path")
        == _render_path(task.expected_path_template, task.expected_path_parameters)
        and request.get("path_template") == task.expected_path_template
        and request.get("path_parameters") == expected_path_parameters
        and request.get("query_parameters") == expected_query
        and request.get("response_status") == expected_status
        for request in requests
    )


def validate_evaluation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise PatchbenchError("Answer evaluation requires schema_version: 1")
    status = value.get("expected_http_status")
    if type(status) is not int or not 100 <= status <= 599:
        raise PatchbenchError("Answer evaluation requires an HTTP status")
    rules = value.get("normalization")
    if not isinstance(rules, list):
        raise PatchbenchError("Answer normalization must be a list")
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"when", "result"}:
            raise PatchbenchError("Normalization rule requires when and result")
        if not isinstance(rule["when"], list) or not rule["when"]:
            raise PatchbenchError("Normalization rule requires conditions")
        for condition in rule["when"]:
            if (not isinstance(condition, dict)
                or not isinstance(condition.get("path"), str)
                or set(condition) not in ({"path", "type"}, {"path", "equals"})):
                raise PatchbenchError("Invalid normalization condition")
            if "type" in condition and condition["type"] not in ("object", "array", "string"):
                raise PatchbenchError("Unsupported normalization type")
            _validate_path(condition["path"])
            if "equals" in condition:
                _validate_template(condition["equals"])
        _validate_template(rule["result"])
    live = value.get("live_response")
    if not isinstance(live, dict) or not isinstance(live.get("path"), str) or live.get("type") not in ("object", "array"):
        raise PatchbenchError("Answer evaluation requires a live_response path and type")
    if re.fullmatch(r"\$(?:\.[A-Za-z_][A-Za-z0-9_-]*)+", live["path"]) is None:
        raise PatchbenchError("live_response path must select object keys, such as $.data.results")
    return value


def _validate_path(path: Any) -> None:
    if not isinstance(path, str) or re.fullmatch(r"\$(?:\.[A-Za-z_][A-Za-z0-9_-]*(?:\[\d+\])?)*", path) is None:
        raise PatchbenchError("Invalid evaluator JSON path")


def _validate_template(value: Any) -> None:
    if isinstance(value, dict):
        if "$path" in value:
            if len(value) != 1:
                raise PatchbenchError("$path must be a standalone template reference")
            _validate_path(value["$path"])
        elif "$parameter" in value:
            if len(value) != 1 or not isinstance(value["$parameter"], str) or not value["$parameter"]:
                raise PatchbenchError("Invalid normalization parameter reference")
        else:
            for item in value.values():
                _validate_template(item)
    elif isinstance(value, list):
        for item in value:
            _validate_template(item)


def _resolve_template(value: Any, answer: Any, parameters: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$path"}:
            found, result = (True, answer) if value["$path"] == "$" else _json_path(answer, value["$path"])
            if not found:
                raise PatchbenchError(f"Normalization path is missing: {value['$path']}")
            return result
        if set(value) == {"$parameter"}:
            if value["$parameter"] not in parameters:
                raise PatchbenchError("Normalization parameter is missing")
            return parameters[value["$parameter"]]
        return {key: _resolve_template(item, answer, parameters) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_template(item, answer, parameters) for item in value]
    return value


def _normalize_answer(task: Any, answer: Any) -> Any:
    types = {"object": dict, "array": list, "string": str}
    for rule in task.evaluation.get("normalization", []):
        matches = True
        for condition in rule["when"]:
            found, observed = (True, answer) if condition["path"] == "$" else _json_path(answer, condition["path"])
            if not found:
                matches = False
                break
            if "type" in condition:
                matches = isinstance(observed, types[condition["type"]])
            else:
                expected = _resolve_template(condition["equals"], answer, task.expected_path_parameters)
                matches = observed is expected if isinstance(expected, bool) else observed == expected
            if not matches:
                break
        if matches:
            return _resolve_template(rule["result"], answer, task.expected_path_parameters)
    return answer
