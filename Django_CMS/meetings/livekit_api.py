"""Talking to LiveKit without its SDK.

Access tokens are HS256 JWTs (PyJWT, already a core dependency) and the server
API is Twirp — JSON over HTTP POST (``requests``, likewise). Keeping the
LiveKit SDK out of the web image is what lets this app cost next to nothing
when it is switched off, and keeps the web image's pinned dependencies out of
a fight with the agent workers' (which run in their own image).

Every call has a short timeout and raises ``LiveKitError`` with a message fit
for a navigator; none is ever made inside a database transaction.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time

import jwt
import requests

from . import config

logger = logging.getLogger(__name__)

TIMEOUT_S = 4


class LiveKitError(Exception):
    """LiveKit could not do what was asked. The message is safe to show staff."""


class RoomGone(LiveKitError):
    """The room does not exist (any more)."""


# ── Tokens ─────────────────────────────────────────────────────────────────

def participant_token(*, identity: str, name: str, room: str, attributes: dict,
                      can_publish_data: bool, sources=("microphone", "camera"),
                      metadata: str = "", ttl_s: int = 600) -> str:
    """A token that joins exactly one room as exactly one identity.

    ``canUpdateOwnMetadata`` is off: the name and the ``cc.*`` attributes set
    here (role, whether to record) are what the recorder and the agents trust,
    so a browser must not be able to rewrite them.
    """
    now = int(time.time())
    grant = {
        "room": room,
        "roomJoin": True,
        "canPublish": True,
        "canSubscribe": True,
        "canPublishData": bool(can_publish_data),
        "canPublishSources": list(sources),
        "canUpdateOwnMetadata": False,
    }
    claims = {
        "iss": config.api_key(),
        "sub": identity,
        "name": name,
        "nbf": now - 5,
        "iat": now,
        "exp": now + int(ttl_s),
        "video": grant,
        "attributes": {k: str(v) for k, v in (attributes or {}).items()},
    }
    if metadata:
        claims["metadata"] = metadata
    return jwt.encode(claims, config.api_secret(), algorithm="HS256")


def _server_token(grants: dict, ttl_s: int = 60) -> str:
    now = int(time.time())
    return jwt.encode({
        "iss": config.api_key(),
        "sub": "conversational-care",
        "nbf": now - 5,
        "exp": now + ttl_s,
        "video": grants,
    }, config.api_secret(), algorithm="HS256")


# ── Twirp ──────────────────────────────────────────────────────────────────

def _twirp(service: str, method: str, body: dict, grants: dict) -> dict:
    if not config.livekit_configured():
        raise LiveKitError("Online meetings are not connected to a LiveKit server yet.")
    url = f"{config.livekit_api_url()}/twirp/livekit.{service}/{method}"
    try:
        resp = requests.post(
            url, json=body, timeout=TIMEOUT_S,
            headers={"Authorization": f"Bearer {_server_token(grants)}",
                     "Content-Type": "application/json"},
        )
    except requests.RequestException as exc:
        logger.warning("LiveKit %s/%s unreachable: %s", service, method, exc)
        raise LiveKitError("The meeting server could not be reached.") from exc
    if resp.status_code == 404 or (resp.status_code >= 400 and '"not_found"' in resp.text):
        raise RoomGone("That room no longer exists.")
    if resp.status_code >= 400:
        logger.warning("LiveKit %s/%s HTTP %s: %s", service, method, resp.status_code,
                       resp.text[:300])
        raise LiveKitError("The meeting server refused the request.")
    try:
        return resp.json() if resp.content else {}
    except ValueError:
        return {}


def _room_admin(room: str = "") -> dict:
    return {"roomAdmin": True, "room": room} if room else {"roomCreate": True, "roomList": True}


def create_room(name: str, *, max_participants: int = 8, empty_timeout_s: int = 300,
                departure_timeout_s: int = 30, metadata: str = "") -> dict:
    return _twirp("RoomService", "CreateRoom", {
        "name": name,
        "emptyTimeout": empty_timeout_s,
        "departureTimeout": departure_timeout_s,
        "maxParticipants": max_participants,
        "metadata": metadata,
    }, {"roomCreate": True})


def delete_room(name: str) -> None:
    try:
        _twirp("RoomService", "DeleteRoom", {"room": name}, {"roomCreate": True})
    except RoomGone:
        pass


def list_participants(room: str) -> list[dict]:
    return _twirp("RoomService", "ListParticipants", {"room": room},
                  _room_admin(room)).get("participants", [])


def list_rooms(names=None) -> list[dict]:
    body = {"names": list(names)} if names else {}
    return _twirp("RoomService", "ListRooms", body, {"roomList": True}).get("rooms", [])


def remove_participant(room: str, identity: str) -> None:
    try:
        _twirp("RoomService", "RemoveParticipant", {"room": room, "identity": identity},
               _room_admin(room))
    except RoomGone:
        pass


def send_data(room: str, payload: dict, *, to: list[str], topic: str) -> None:
    """A reliable data message from the server to named participants only."""
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    _twirp("RoomService", "SendData", {
        "room": room, "data": data, "kind": 0,  # RELIABLE
        "destinationIdentities": list(to), "topic": topic,
    }, _room_admin(room))


def create_dispatch(room: str, agent_name: str, metadata: dict) -> str:
    """Ask a named agent worker to join ``room``. Returns the dispatch id.

    The metadata reaches the job and nobody else — unlike room or participant
    metadata, which every participant can read — which is why the agent's
    credentials for calling back into Django travel here.
    """
    out = _twirp("AgentDispatchService", "CreateDispatch", {
        "room": room, "agentName": agent_name, "metadata": json.dumps(metadata),
    }, _room_admin(room))
    return out.get("id", "")


def delete_dispatch(room: str, dispatch_id: str) -> None:
    if not dispatch_id:
        return
    try:
        _twirp("AgentDispatchService", "DeleteDispatch",
               {"room": room, "dispatchId": dispatch_id}, _room_admin(room))
    except RoomGone:
        pass


def ping() -> bool:
    """Can we reach LiveKit and does it accept our key pair?"""
    try:
        _twirp("RoomService", "ListRooms", {"names": ["cc-health-check"]}, {"roomList": True})
        return True
    except LiveKitError:
        return False


# ── Webhooks ───────────────────────────────────────────────────────────────

def verify_webhook(body: bytes, auth_header: str) -> dict | None:
    """The webhook's event, or None if it was not signed by our LiveKit.

    LiveKit signs each delivery with a JWT (our key pair) whose ``sha256``
    claim is the base64 SHA-256 of the raw body, so a valid token cannot be
    replayed against a different body.
    """
    token = (auth_header or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or not config.api_secret():
        return None
    try:
        claims = jwt.decode(token, config.api_secret(), algorithms=["HS256"],
                            options={"verify_aud": False}, leeway=30)
    except jwt.PyJWTError:
        return None
    if claims.get("iss") != config.api_key():
        return None
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
    if claims.get("sha256") != digest:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
