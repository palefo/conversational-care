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
        scheduled_protocols=[2, 3],   # optional protocol numbers
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
        scheduled_protocols: Optional[List[int]] = None,
        scheduled_protocol: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Schedule a meeting for a client.

        ``scheduled_protocols`` is a list of protocol *numbers* the call should
        cover. ``scheduled_protocol`` is the single-value spelling this method
        shipped with; it still works and means a list of one. A number with no
        protocol behind it is rejected with a 400 rather than stored.

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
        if scheduled_protocols:
            payload["scheduled_protocols"] = list(scheduled_protocols)
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

    # -- conversations -------------------------------------------------------
    # The two endpoints an agent calls back about a conversation it is holding.
    # Both refuse with a 404 rather than a 403 when you may not touch the
    # conversation — a caller who has no business with it should not learn from
    # the status code whether it exists — so ``None`` here means "not yours, not
    # there, or the feature is switched off", and the three are deliberately
    # indistinguishable.
    #
    # Who may call them: the account that holds the conversation, the tester
    # account standing in for the client, or an admin. A navigator deliberately
    # cannot set visibility — it is the client's own answer about their own
    # privacy, and a link worker setting it on their behalf would make it worth
    # nothing.

    def get_conversation_visibility(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Whether this conversation's content is hidden from the client's link worker.

        Returns ``{conversation_id, hidden, hidden_at, message_count}``, or
        ``None`` if the conversation is not yours to read (see the note above).
        """
        resp = self.session.get(
            self._url(f"/api/v1/conversations/{conversation_id}/visibility/"),
            timeout=self.timeout,
        )
        if resp.status_code == 404:
            return None
        return self._json_or_raise(resp)

    def set_conversation_visibility(self, conversation_id: str,
                                    hidden: bool) -> Optional[Dict[str, Any]]:
        """Hide this conversation from the client's link worker, or unhide it.

        Only ever on the client's own say-so, and only after telling them what it
        means: their link worker still sees that the conversation happened, when
        it was and how many messages it had, and still reads the summary — what
        they lose is the messages, the topic and the review. A supervising
        administrator can still read all of it, and a conversation suggesting the
        person may be at risk of harming themselves stays readable whatever they
        asked, because somebody has to be able to help.

        Idempotent: the response describes the state the conversation is now in
        rather than what changed, so asking twice is not an error.

        ``message_count`` comes back so you can tell the client exactly what
        their link worker is left with, in the same breath as confirming it.
        """
        resp = self.session.post(
            self._url(f"/api/v1/conversations/{conversation_id}/visibility/"),
            json={"hidden": bool(hidden)}, timeout=self.timeout,
        )
        if resp.status_code == 404:
            return None
        return self._json_or_raise(resp)

    def get_conversation_summary(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """The summary the client's link worker reads for this conversation.

        Returns ``{conversation_id, summary, source, agent_summary,
        agent_summary_at, hidden}``, or ``None`` if the conversation is not yours
        to read. ``source`` is ``"agent"`` or ``"classifier"`` — or ``""`` when
        nothing has summarised it yet — so you can tell whether your own report
        is the one on screen.
        """
        resp = self.session.get(
            self._url(f"/api/v1/conversations/{conversation_id}/summary/"),
            timeout=self.timeout,
        )
        if resp.status_code == 404:
            return None
        return self._json_or_raise(resp)

    def report_conversation_summary(self, conversation_id: str,
                                    summary: str) -> Optional[Dict[str, Any]]:
        """Record what this conversation was about, for the client's link worker.

        Write it for the link worker, who was not there and will read it to pick
        up where you left off — not for the person you were talking to. What they
        wanted, what you told them, what is still open.

        Preferred over the platform's own automatic summary, and shown even when
        the conversation is hidden: on a hidden conversation this is the *only*
        thing the link worker gets, so it must be something the client would
        expect them to read.

        Each call replaces the last, so the text should stand on its own. An
        empty summary is rejected with a 400 rather than erasing one somebody may
        already have read.
        """
        resp = self.session.post(
            self._url(f"/api/v1/conversations/{conversation_id}/summary/"),
            json={"summary": summary}, timeout=self.timeout,
        )
        if resp.status_code == 404:
            return None
        return self._json_or_raise(resp)



# The run-token client lives in its own module so a remote agent can vendor just
# run_client.py + langgraph_tools.py without this file. Re-exported here so
# ``from conversationalcare_api import RunClient`` keeps working.
from .run_client import RunClient  # noqa: E402,F401
