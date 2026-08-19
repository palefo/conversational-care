"""Thin Python client for the Conversational Care REST API.

Authentication uses your personal API token (see the Settings → API client page
in the app, or your Profile page, for how to generate one). All calls are made
with the header ``Authorization: Token <your-token>`` and are subject to the
same per-user permissions as the web app: navigators see/act on their own
clients; admins see everybody.

Example
-------
    from conversationalcare_api import Client

    client = Client(base_url="https://your-platform.example.com", token="abc123...")

    # List clients you can see (optionally filter by name / last name / phone)
    for p in client.list_patients():
        print(p["id"], p["name"], p["lastname"], p["phone_number"])

    # Schedule a meeting for a client
    result = client.schedule_meeting(
        patient_id=10,
        scheduled_time_iso="2026-08-01T15:30:00Z",
        type=1,                 # 0 Onboarding, 1 Regular, 2 Final, 3 Initial
        scheduled_protocol=2,   # optional protocol number
    )
    if result.get("ok"):
        print("Scheduled meeting", result["meeting"]["id"])
    else:
        print("Could not schedule:", result.get("detail"))
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests


class ConversationalCareError(RuntimeError):
    """Raised when the API returns an unexpected error response."""


class Client:
    """Client for the Conversational Care API.

    Parameters
    ----------
    base_url:
        Root URL of your platform, e.g. ``https://your-platform.example.com``.
    token:
        Your personal API token (generate it on the Profile page).
    timeout:
        Per-request timeout in seconds (default 30).
    """

    def __init__(self, base_url: str, token: str, timeout: int = 30) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not token:
            raise ValueError("token is required")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {token}",
            "Accept": "application/json",
        })

    # -- internals -----------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _json_or_raise(self, resp: "requests.Response") -> Any:
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400 and resp.status_code not in (403, 409):
            detail = data if data is not None else resp.text
            raise ConversationalCareError(
                f"API error {resp.status_code}: {detail}"
            )
        return data

    # -- endpoints -----------------------------------------------------------
    def list_patients(self, q: Optional[str] = None) -> List[Dict[str, Any]]:
        """List clients visible to you, optionally filtered by ``q``.

        ``q`` matches name, last name, full name or phone (substring).
        Returns a list of ``{id, name, lastname, phone_number}`` dicts.
        """
        params = {"q": q} if q else None
        resp = self.session.get(self._url("/api/v1/patients/"), params=params, timeout=self.timeout)
        return self._json_or_raise(resp) or []

    def schedule_meeting(
        self,
        patient_id: int,
        scheduled_time_iso: str,
        type: Optional[int] = None,
        scheduled_protocol: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Schedule a meeting for a client.

        Returns ``{"ok": True, "meeting": {...}}`` on success, or
        ``{"ok": False, "detail": "..."}`` if it was rejected (e.g. a
        scheduling conflict, or you don't own that client).
        """
        payload: Dict[str, Any] = {
            "patient_id": patient_id,
            "scheduled_time": scheduled_time_iso,
        }
        if type is not None:
            payload["type"] = type
        if scheduled_protocol is not None:
            payload["scheduled_protocol"] = scheduled_protocol
        resp = self.session.post(self._url("/api/v1/meetings/"), json=payload, timeout=self.timeout)
        data = self._json_or_raise(resp)
        if isinstance(data, dict):
            return data
        return {"ok": False, "detail": f"Unexpected response ({resp.status_code})."}

    def list_patient_meetings(self, patient_id: int) -> List[Dict[str, Any]]:
        """List the meetings scheduled for a given client."""
        resp = self.session.get(
            self._url(f"/api/v1/patients/{patient_id}/meetings/"), timeout=self.timeout
        )
        return self._json_or_raise(resp) or []
