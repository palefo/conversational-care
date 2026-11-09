"""HTTP calls to a Field App installation (its /connect and /api/v1 endpoints).

Kept to plain ``requests`` (already a platform dependency). The browser-facing
URL is ``installation.browser_root``; every server-to-server call uses
``installation.api_root``.
"""
from __future__ import annotations

from urllib.parse import urlencode

import requests

TIMEOUT = 8


class FieldAppError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(f"Field App answered {status}: {detail}")
        self.status = status
        self.detail = detail

    @property
    def unauthorized(self):
        return self.status == 401


def _check(resp):
    if resp.ok:
        return resp
    try:
        detail = resp.json()
    except ValueError:
        detail = resp.text[:200]
    raise FieldAppError(resp.status_code, detail)


def authorize_url(installation, redirect_uri: str, state: str) -> str:
    query = urlencode({"client_id": installation.client_id, "redirect_uri": redirect_uri, "state": state})
    return f"{installation.browser_root}/connect/authorize/?{query}"


def exchange_code(installation, code: str, redirect_uri: str) -> dict:
    """The one-time code from the redirect, plus our secret → the user's token."""
    try:
        resp = requests.post(f"{installation.api_root}/api/v1/connect/token/", json={
            "client_id": installation.client_id, "client_secret": installation.client_secret,
            "code": code, "redirect_uri": redirect_uri,
        }, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise FieldAppError(0, str(exc)) from exc
    return _check(resp).json()


class Api:
    """Read-only calls made with one user's connection token."""

    def __init__(self, installation, token: str):
        self.root = installation.api_root + "/api/v1"
        self.headers = {"Authorization": f"Bearer {token}"}

    def _get(self, path, **params):
        try:
            resp = requests.get(f"{self.root}/{path}", headers=self.headers,
                                params={k: v for k, v in params.items() if v}, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise FieldAppError(0, str(exc)) from exc
        return _check(resp)

    def me(self) -> dict:
        return self._get("me/").json()

    def search(self, q: str = "", dob: str = "") -> list:
        return self._get("patients/", q=q, dob=dob).json()

    def athlete(self, athlete_id: int) -> dict:
        return self._get(f"patients/{athlete_id}/").json()

    def context_markdown(self, athlete_id: int, rounds: int = 1) -> str:
        return self._get(f"patients/{athlete_id}/context/", **{"as": "markdown", "rounds": rounds}).text

    def revoke(self) -> None:
        try:
            requests.post(f"{self.root}/connect/revoke/", headers=self.headers, timeout=TIMEOUT)
        except requests.RequestException:
            pass  # best effort; the token is deleted here either way
