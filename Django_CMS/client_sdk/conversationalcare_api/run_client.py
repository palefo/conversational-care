"""The client a **remote agent** uses to call back about the run it is in.

Self-contained on purpose — it needs only ``requests`` — so an agent server can
copy this file and ``langgraph_tools.py`` into its own tree without the rest of
the SDK. See ``RunClient`` below, and agent_tools.md in Conversational Care.
"""
from typing import Any, Dict, Optional

import requests

try:
    from .Client import ConversationalCareError
except ImportError:  # vendored without the full SDK
    class ConversationalCareError(RuntimeError):
        """Raised when the API returns an unexpected error response."""

__all__ = ["RunClient", "ConversationalCareError"]


class RunClient:
    """For a **remote agent** calling back about the run it is in.

    Conversational Care puts a ``cc_run_token`` in the run config of every run of
    a remote agent that has *Allow callbacks* switched on. The token names one
    conversation, allows only reporting its summary and setting its visibility,
    and expires after two hours. The endpoints take no conversation id — the
    token says which — so there is nothing here to pass, forge or get wrong.

    Use :meth:`from_config` inside a tool::

        client = RunClient.from_config(config)
        if client:
            client.report_summary("What they wanted, what was said, what is open.")

    Never use a personal API token for this: it is a whole person's access to
    every client, and the run config is stored by the agent server.
    """

    def __init__(self, base_url: str, run_token: str, timeout: int = 15) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not run_token:
            raise ValueError("run_token is required")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"RunToken {run_token}",
            "Accept": "application/json",
        })

    @classmethod
    def from_config(cls, config: Any, base_url: Optional[str] = None,
                    timeout: int = 15) -> Optional["RunClient"]:
        """A client for this run, or ``None`` when the run carries no token.

        ``None`` is the normal answer under ``langgraph dev``, in tests, and for
        an agent whose callbacks are switched off — tools should say so to the
        model rather than fail. The base URL comes from ``cc_api_url`` in the
        run config if the platform sent one, else ``base_url``, else the
        ``CONVERSATIONAL_CARE_BASE_URL`` environment variable.
        """
        import os

        if isinstance(config, dict):
            cfg = config.get("configurable") or {}
        else:
            cfg = getattr(config, "configurable", None) or {}
        token = (cfg or {}).get("cc_run_token")
        url = ((cfg or {}).get("cc_api_url") or base_url
               or os.getenv("CONVERSATIONAL_CARE_BASE_URL") or "")
        if not token or not url:
            return None
        return cls(url, token, timeout=timeout)

    def _call(self, method: str, path: str, payload: Optional[dict] = None):
        resp = self.session.request(method, f"{self.base_url}/api/v1/run/{path}",
                                    json=payload, timeout=self.timeout)
        if resp.status_code == 404:
            return None
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400:
            raise ConversationalCareError(f"API error {resp.status_code}: {data or resp.text}")
        return data

    def get(self) -> Optional[Dict[str, Any]]:
        """This run's conversation: ``summary``, ``source``, ``hidden``, ``privacy_available``…"""
        return self._call("GET", "")

    def report_summary(self, summary: str) -> Optional[Dict[str, Any]]:
        """Record what the conversation was about, for the client's link worker.

        Replaces any summary reported before. Blank or over 2000 characters is
        refused with :class:`ConversationalCareError`.
        """
        return self._call("POST", "summary/", {"summary": summary})

    def set_visibility(self, hidden: bool) -> Optional[Dict[str, Any]]:
        """Hide this conversation from the client's link worker, or unhide it.

        Only on the client's own say-so. Returns ``None`` if privacy is switched
        off on this installation.
        """
        return self._call("POST", "visibility/", {"hidden": bool(hidden)})
