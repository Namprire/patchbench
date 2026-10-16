"""Minimal read-only GitLab client used only during case ingestion."""

from __future__ import annotations

import json
import os
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from patchbench import __version__
from patchbench.exceptions import PatchbenchError


DEFAULT_GITLAB_URL = "https://gitlab.example.invalid"


class GitLabClient:
    """Read merge request metadata without exposing authentication details."""

    def __init__(
        self,
        gitlab_url: str,
        token: str,
        *,
        timeout_seconds: float = 30.0,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if not token:
            raise PatchbenchError("GITLAB_TOKEN is required for GitLab access")
        parsed_url = urlsplit(gitlab_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise PatchbenchError(
                "GitLab URL must be an HTTP(S) base URL without credentials, query, or fragment"
            )
        self.gitlab_url = gitlab_url.rstrip("/")
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    @classmethod
    def from_environment(cls, gitlab_url: str | None = None) -> "GitLabClient":
        token = os.environ.get("GITLAB_TOKEN", "")
        url = gitlab_url or os.environ.get("GITLAB_URL", DEFAULT_GITLAB_URL)
        return cls(url, token)

    def fetch_merge_request(self, project_id: str | int, mr_iid: int) -> dict[str, Any]:
        merge_request = self.fetch_merge_request_details(project_id, mr_iid)
        project = quote(str(project_id), safe="")
        base_path = f"projects/{project}/merge_requests/{mr_iid}"
        related = self._optional_list(f"{base_path}/related_issues")
        closing = self._optional_list(f"{base_path}/closes_issues")
        result = dict(merge_request)
        result["related_issues"] = _deduplicate_issues([*related, *closing])
        return result

    def list_merged_merge_requests(
        self, project_id: str | int, limit: int, *, labels: list[str] | None = None,
        start_page: int = 1, page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """List recent merged MRs without fetching their diff content."""
        if limit <= 0 or start_page <= 0 or not 1 <= page_size <= 100:
            raise PatchbenchError("GitLab merge request limit must be greater than zero")

        project = quote(str(project_id), safe="")
        merge_requests: list[dict[str, Any]] = []
        page = start_page
        while len(merge_requests) < limit:
            per_page = min(page_size, limit - len(merge_requests))
            parameters = {
                    "state": "merged",
                    "order_by": "updated_at",
                    "sort": "desc",
                    "per_page": per_page,
                    "page": page,
                }
            if labels:
                parameters["labels"] = ",".join(labels)
            query = urlencode(parameters)
            payload = self._get_json(
                f"projects/{project}/merge_requests?{query}"
            )
            if not isinstance(payload, list):
                raise PatchbenchError(
                    "GitLab merge request list response was not a JSON array"
                )
            page_items = [item for item in payload if isinstance(item, dict)]
            merge_requests.extend(page_items)
            if len(payload) < per_page:
                break
            page += 1
        return merge_requests[:limit]

    def fetch_merge_request_details(
        self, project_id: str | int, mr_iid: int
    ) -> dict[str, Any]:
        """Fetch a single MR's metadata, excluding its diff content."""
        project = quote(str(project_id), safe="")
        base_path = f"projects/{project}/merge_requests/{mr_iid}"
        merge_request = self._get_json(base_path)
        if not isinstance(merge_request, dict):
            raise PatchbenchError("GitLab merge request response was not a JSON object")
        return merge_request

    def redact(self, text: Any) -> str:
        return str(text).replace(self._token, "<redacted>")

    def _optional_list(self, path: str) -> list[dict[str, Any]]:
        try:
            payload = self._get_json(path)
        except _OptionalEndpointUnavailable:
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _get_json(self, path: str) -> Any:
        url = f"{self.gitlab_url}/api/v4/{path.lstrip('/')}"
        request = Request(
            url,
            headers={
                "PRIVATE-TOKEN": self._token,
                "Accept": "application/json",
                "User-Agent": f"patchbench/{__version__}",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {403, 404} and path.endswith(
                ("/related_issues", "/closes_issues")
            ):
                raise _OptionalEndpointUnavailable from exc
            raise PatchbenchError(
                f"GitLab API request failed with HTTP {exc.code}: {url}"
            ) from exc
        except URLError as exc:
            raise PatchbenchError(f"GitLab API request failed for {url}") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PatchbenchError(f"GitLab API returned invalid JSON for {url}") from exc


class _OptionalEndpointUnavailable(Exception):
    pass


def _deduplicate_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for issue in issues:
        key = (issue.get("project_id"), issue.get("iid", issue.get("id")))
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                key: issue.get(key)
                for key in (
                    "project_id",
                    "iid",
                    "title",
                    "description",
                    "references",
                    "web_url",
                )
                if issue.get(key) is not None
            }
        )
    return result
