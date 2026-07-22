"""Client for Plane's public REST API (work items / issues).

Unlike pages, work items are fully served by the CE public API, so there is no
DB access and no Yjs converter here: we send ``description_html`` and Plane
populates the other representations itself.

Auth: header ``x-api-key: <PAT>``. Base URL is the instance site root; the
client appends ``/api/v1``. Rate limiting (API_KEY_RATE_LIMIT, default
60/minute) surfaces 429 as a clear error — never a silent retry loop.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx


class RestError(RuntimeError):
    """A REST call failed (non-2xx that isn't a rate limit)."""


class RateLimitError(RestError):
    """HTTP 429 — the instance's API_KEY_RATE_LIMIT was hit."""


class RestNotFound(RestError):
    """A named/looked-up resource does not exist (surfaced with valid options)."""


PRIORITIES = ("urgent", "high", "medium", "low", "none")


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


class PlaneREST:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/") + "/api/v1"
        self._client = httpx.Client(
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    # --- low-level -----------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self._base}/{path.lstrip('/')}"
        try:
            resp = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise RestError(f"{method} {url} failed: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(
                "Plane REST rate limit hit (HTTP 429; instance API_KEY_RATE_LIMIT, "
                "default 60/minute). Slow down and retry — not retrying automatically."
            )
        if resp.status_code == 404:
            raise RestNotFound(f"{method} {url} -> 404 Not Found")
        if not (200 <= resp.status_code < 300):
            raise RestError(
                f"{method} {url} -> {resp.status_code}: {resp.text[:400]}"
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def get(self, path: str, **kwargs) -> Any:
        return self._request("GET", path, **kwargs)

    def _paginated(self, path: str, **kwargs) -> list[dict]:
        """Return a flat list whether the endpoint paginates or returns a bare list."""
        data = self.get(path, **kwargs)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "results" in data:
            return list(data["results"])
        return [data] if data else []

    def status_of(self, method: str, path: str, **kwargs) -> int:
        """For verify: return the raw status code without raising."""
        url = f"{self._base}/{path.lstrip('/')}"
        try:
            return self._client.request(method, url, **kwargs).status_code
        except httpx.HTTPError:
            return 0

    # --- workspace/project level --------------------------------------

    def list_projects(self, slug: str) -> list[dict]:
        return self._paginated(f"workspaces/{slug}/projects/")

    def resolve_project(self, slug: str, project_ref: str) -> dict:
        for p in self.list_projects(slug):
            if _is_uuid(project_ref) and str(p.get("id")) == project_ref:
                return p
            if str(p.get("identifier", "")).upper() == project_ref.upper():
                return p
        idents = [p.get("identifier") for p in self.list_projects(slug)]
        raise RestNotFound(
            f"project {project_ref!r} not found in workspace {slug!r}; "
            f"available: {idents}"
        )

    # --- issues --------------------------------------------------------

    def list_issues(self, slug: str, project_id: str) -> list[dict]:
        return self._paginated(f"workspaces/{slug}/projects/{project_id}/issues/")

    def get_issue(self, slug: str, project_id: str, issue_id: str) -> dict:
        return self.get(f"workspaces/{slug}/projects/{project_id}/issues/{issue_id}/")

    def resolve_issue_id(self, slug: str, project_id: str, item_ref: str) -> str:
        """Accept a UUID or a sequence ref like TEST-42 and return the issue UUID."""
        if _is_uuid(item_ref):
            return item_ref
        seq = _parse_sequence(item_ref)
        for issue in self.list_issues(slug, project_id):
            if issue.get("sequence_id") == seq:
                return str(issue["id"])
        raise RestNotFound(f"work item {item_ref!r} not found in project")

    def create_issue(self, slug: str, project_id: str, payload: dict) -> dict:
        return self._request(
            "POST", f"workspaces/{slug}/projects/{project_id}/issues/", json=payload
        )

    def update_issue(self, slug: str, project_id: str, issue_id: str, payload: dict) -> dict:
        return self._request(
            "PATCH",
            f"workspaces/{slug}/projects/{project_id}/issues/{issue_id}/",
            json=payload,
        )

    # --- lookups (states / labels / members) ---------------------------

    def list_states(self, slug: str, project_id: str) -> list[dict]:
        return self._paginated(f"workspaces/{slug}/projects/{project_id}/states/")

    def list_labels(self, slug: str, project_id: str) -> list[dict]:
        return self._paginated(f"workspaces/{slug}/projects/{project_id}/labels/")

    def list_members(self, slug: str, project_id: str) -> list[dict]:
        return self._paginated(f"workspaces/{slug}/projects/{project_id}/members/")

    def list_cycles(self, slug: str, project_id: str) -> list[dict]:
        return self._paginated(f"workspaces/{slug}/projects/{project_id}/cycles/")

    def list_modules(self, slug: str, project_id: str) -> list[dict]:
        return self._paginated(f"workspaces/{slug}/projects/{project_id}/modules/")


def _parse_sequence(item_ref: str) -> int:
    tail = item_ref.rsplit("-", 1)[-1]
    try:
        return int(tail)
    except ValueError as exc:
        raise RestNotFound(
            f"{item_ref!r} is neither a UUID nor a SEQ ref like TEST-42"
        ) from exc


# --- name -> UUID resolution helpers (raise with valid options) --------


def resolve_named(items: list[dict], name: str, *, key: str = "name", what: str) -> str:
    """Case-insensitive lookup of ``name`` in ``items``; return the item's id."""
    for it in items:
        if str(it.get(key, "")).strip().lower() == name.strip().lower():
            return str(it["id"])
    valid = [it.get(key) for it in items]
    raise RestNotFound(f"unknown {what} {name!r}; valid options: {valid}")


def member_display_map(members: list[dict]) -> dict[str, str]:
    """member id -> a human display name, tolerant of the member payload shape."""
    out: dict[str, str] = {}
    for m in members:
        mid = m.get("member") or m.get("id") or m.get("member_id")
        if mid is None:
            continue
        name = (
            m.get("display_name")
            or m.get("member__display_name")
            or m.get("email")
            or m.get("member__email")
            or str(mid)
        )
        out[str(mid)] = name
    return out


def resolve_member(members: list[dict], name: str) -> str:
    """Resolve an assignee by display name or email -> member id."""
    wanted = name.strip().lower()
    for m in members:
        mid = m.get("member") or m.get("id") or m.get("member_id")
        for field in ("display_name", "member__display_name", "email", "member__email"):
            if str(m.get(field, "")).strip().lower() == wanted and mid is not None:
                return str(mid)
    valid = [
        m.get("display_name") or m.get("member__display_name") or m.get("email")
        for m in members
    ]
    raise RestNotFound(f"unknown assignee {name!r}; valid options: {valid}")
