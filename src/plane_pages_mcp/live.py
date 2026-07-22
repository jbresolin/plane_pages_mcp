"""Client for Plane's internal live-service HTML->Yjs converter.

The endpoint (apps/live/src/controllers/document.controller.ts) has NO auth and
must stay reachable only inside the compose network. It returns the JSON and the
base64-encoded Yjs binary that the editor treats as authoritative.
"""

from __future__ import annotations

import base64

import httpx


class ConvertError(RuntimeError):
    """Raised when the convert endpoint fails or returns an unexpected body."""


class LiveConverter:
    def __init__(self, convert_url: str, timeout: float = 30.0) -> None:
        self._url = convert_url
        self._timeout = timeout

    def convert(self, description_html: str, variant: str = "rich") -> tuple[dict, bytes]:
        """POST HTML, return (description_json, description_binary_bytes).

        Pages use variant "rich". Raises ConvertError with the endpoint's own
        error body on any non-200 so the caller can abort before touching the DB.
        """
        try:
            resp = httpx.post(
                self._url,
                json={"description_html": description_html, "variant": variant},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise ConvertError(f"convert request to {self._url} failed: {exc}") from exc

        if resp.status_code != 200:
            raise ConvertError(
                f"convert endpoint returned {resp.status_code}: {resp.text[:500]}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise ConvertError(
                f"convert endpoint returned non-JSON body: {resp.text[:200]}"
            ) from exc

        if "description_json" not in data or "description_binary" not in data:
            raise ConvertError(
                f"convert response missing keys; got {sorted(data)}"
            )

        try:
            binary = base64.b64decode(data["description_binary"])
        except (ValueError, TypeError) as exc:
            raise ConvertError(f"description_binary is not valid base64: {exc}") from exc

        if not binary:
            raise ConvertError("convert returned empty description_binary")

        return data["description_json"], binary
