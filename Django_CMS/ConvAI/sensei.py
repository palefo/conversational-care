"""Adapter for **Sensei** agents — see agents.md → "Sensei agents".

Sensei is an external service (a KIST-run Azure Function) that answers health
questions from the wearable/phone data a person has synced to it. Conversational
Care never touches that data and never shares a database with Sensei: the whole
integration is one JSON POST per turn.

The contract, verified against the deployed endpoint::

    POST <SENSEI_API_URL>
    Content-Type: application/json
    x-functions-key: <SENSEI_FUNCTION_KEY>

    {"operation": "message",  "external_user_id": "cc_<hex>",
     "conversation_id": "<uuid>", "message": "<text>"}
    {"operation": "login",    "external_user_id": "cc_<hex>",
     "app_user_id": "<id>", "passcode": "<code>"}
    {"operation": "register", "external_user_id": "cc_<hex>",
     "app_user_id": "<id>", "passcode": "<code>"}

    -> {"status": "ok" | "error", "response": "<text to show the user>"}

Two properties of that contract shape everything below.

*Sensei holds the session.* A successful ``login``/``register`` binds the
Sensei-side account to our ``external_user_id``, and later ``message`` calls
just work. So Conversational Care stores no Sensei credentials at all — which
is why the passcode can be scrubbed the moment the turn has been forwarded.

*The error text is the user-facing text.* A 401 answers "Please use /login
… first", which is exactly what the caregiver needs to read. So a non-200 with
a ``response`` body is relayed verbatim rather than swallowed into a generic
failure message.
"""
import hashlib
import hmac
import json
import logging
import re

import requests

from .site_config import get_bool, get_setting

logger = logging.getLogger(__name__)

# Long on purpose. A Sensei turn can fan out into several queries over a
# person's health history, and the deployed endpoint regularly takes tens of
# seconds to answer. Under the synchronous WhatsApp webhook this is the ceiling
# on how long Twilio is kept waiting, so installations that use Sensei on
# WhatsApp should run with ASYNC_WHATSAPP_REPLY on (see async_replies.md).
REQUEST_TIMEOUT = 120

# Sensei validates that the caller-supplied id is opaque and namespaced to us.
_ID_PREFIX = "cc_"

# The two credential-bearing commands, as the user types them (the names come
# from Sensei's own user guide). `/register_user` maps to the API's `register`.
_OPERATION_FOR_COMMAND = {
    "login": "login",
    "register_user": "register",
}

# Matches the *command*, not its arguments — deliberately loose. Recognising a
# credential line and parsing one are different questions: "/login ada" is a
# typo that must still be recognised (so it can be answered with help instead of
# forwarded as chat), and "/login ada pass oops" must still be recognised (so
# the passcode in it is redacted rather than stored raw for want of a match).
_CREDENTIAL_RE = re.compile(r"^\s*/(login|register_user)\b(.*)$",
                            re.IGNORECASE | re.DOTALL)

# What a redacted passcode is replaced with in stored messages and logs. Kept
# visible rather than dropped: a navigator reading the transcript should be able
# to see that the caregiver logged in, just not what they logged in with.
REDACTED = "••••"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def enabled() -> bool:
    """Whether this installation may use Sensei agents at all.

    Off by default. Everything Sensei-shaped in the UI — the settings tab, the
    agent kind, the create button — hangs off this, so an installation with no
    Sensei service never sees the feature.
    """
    return get_bool("SENSEI_ENABLED", False)


def api_url() -> str:
    return (get_setting("SENSEI_API_URL", "") or "").strip()


def _function_key() -> str:
    return (get_setting("SENSEI_FUNCTION_KEY", "") or "").strip()


def _id_secret() -> str:
    return (get_setting("SENSEI_USER_ID_SECRET", "") or "").strip()


def configured() -> tuple[bool, str]:
    """``(ok, reason)`` — whether a Sensei call could succeed right now.

    Reported from the same values :func:`send` uses, so the settings page cannot
    claim Sensei is ready while every turn is failing on a missing value.
    """
    if not enabled():
        return False, "Sensei is disabled for this installation."
    if not api_url():
        return False, "No Sensei API URL is set."
    if not _function_key():
        return False, "No Sensei function key is set."
    if not _id_secret():
        return False, "No Sensei user-id secret is set."
    return True, ""


# ---------------------------------------------------------------------------
# Opaque per-patient identity
# ---------------------------------------------------------------------------
def _identity(subject) -> str:
    """``<model>:<pk>`` for the thing we are speaking on behalf of.

    Almost always a Patient. The agent test chat passes the logged-in admin
    instead, and the two primary-key spaces overlap — patient 3 and user 3 are
    different people — so the model label has to be part of what is hashed, or
    an admin testing the agent would land in some patient's Sensei account.
    """
    meta = getattr(subject, "_meta", None)
    label = meta.label_lower if meta is not None else type(subject).__name__.lower()
    return f"{label}:{subject.pk}"


def external_user_id(subject) -> str:
    """A stable, opaque id for ``subject`` (normally a Patient) to send to Sensei.

    HMAC rather than a plain hash: the primary keys are small integers, so an
    unkeyed digest of one is trivially reversible by anyone holding the endpoint
    — which would hand Sensei (and anything reading its logs) a working map back
    to our patient rows. With the secret, the id is meaningless outside this
    installation.

    Stable for the life of the secret, which is what lets a caregiver log in
    once and stay logged in across conversations.
    """
    secret = _id_secret()
    if not secret:
        raise RuntimeError("SENSEI_USER_ID_SECRET is not set")
    digest = hmac.new(
        secret.encode("utf-8"),
        _identity(subject).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{_ID_PREFIX}{digest}"


# ---------------------------------------------------------------------------
# Credential commands
# ---------------------------------------------------------------------------
def parse_command(text: str) -> dict | None:
    """Parse a `/login` or `/register_user` line into a structured operation.

    Returns ``None`` for anything that is not one of those commands — including
    a malformed one, which is handled by :func:`command_help` so the user is
    told the shape rather than having their typo forwarded to Sensei as chat.
    """
    match = _CREDENTIAL_RE.match(text or "")
    if not match:
        return None
    # Exactly two arguments. An AppUserId or passcode containing a space cannot
    # be told apart from a third argument, so those are refused rather than
    # guessed at — a wrong guess would send the wrong passcode to Sensei.
    arguments = match.group(2).split()
    if len(arguments) != 2:
        return None
    return {
        "operation": _OPERATION_FOR_COMMAND[match.group(1).lower()],
        "app_user_id": arguments[0],
        "passcode": arguments[1],
    }


def is_credential_command(text: str) -> bool:
    """Whether ``text`` is a `/login` / `/register_user` line, valid or not."""
    return _CREDENTIAL_RE.match(text or "") is not None


def command_help() -> str:
    return ("To use Sensei, send:\n"
            "/login <AppUserId> <Passcode>\n"
            "or, if you do not have an account yet:\n"
            "/register_user <NewAppUserId> <NewPasscode>")


def redact(text: str) -> str:
    """Strip the passcode out of a `/login` / `/register_user` line.

    Applied to *every* inbound message before it is stored, not only to messages
    bound for a Sensei agent: a caregiver who types their passcode at the wrong
    agent has still typed their passcode, and the transcript is read by
    navigators and swept by the classifier either way.

    The AppUserId is kept — it is an account name, and seeing which account a
    caregiver logged in as is exactly what makes a failed login diagnosable.
    """
    match = _CREDENTIAL_RE.match(text or "")
    if not match:
        return text
    command = match.group(1).lower()
    arguments = match.group(2).split()
    if not arguments:
        return f"/{command}"
    if len(arguments) == 1:
        # Just the account name — a typo, with no passcode in it to remove.
        return f"/{command} {arguments[0]}"
    # Everything after the account name goes, however many words it ran to: a
    # mistyped line is exactly where a passcode ends up in an unexpected place.
    return f"/{command} {arguments[0]} {REDACTED}"


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------
def _post(payload: dict) -> str:
    """POST one operation and return the text Sensei wants the user to see."""
    try:
        resp = requests.post(
            api_url(),
            headers={
                "Content-Type": "application/json",
                "x-functions-key": _function_key(),
            },
            data=json.dumps(payload),
            timeout=REQUEST_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("Sensei timed out after %ss (operation=%s)",
                       REQUEST_TIMEOUT, payload.get("operation"))
        return "Sorry, Sensei took too long to answer. Please try again."
    except requests.RequestException:
        logger.exception("Could not reach Sensei (operation=%s)", payload.get("operation"))
        return "Sorry, could not reach Sensei."

    try:
        body = resp.json()
    except ValueError:
        logger.warning("Sensei returned non-JSON (HTTP %s)", resp.status_code)
        return "Sorry, Sensei returned an unreadable response."

    # Relayed on success *and* on failure: Sensei's error strings are written
    # for the person on the other end ("Please use /login … first"), and
    # replacing them with our own would take away the only instruction that
    # tells them how to get unstuck.
    reply = (body.get("response") or "").strip() if isinstance(body, dict) else ""
    if reply:
        if resp.status_code >= 400:
            logger.info("Sensei refused an operation (HTTP %s, operation=%s)",
                        resp.status_code, payload.get("operation"))
        return reply

    logger.warning("Sensei returned no response text (HTTP %s)", resp.status_code)
    return "Sorry, something went wrong talking to Sensei."


def send(subject, text: str, thread_id: str | None = None) -> str:
    """Forward one turn for ``subject`` (a Patient) to Sensei and return the reply.

    ``/login`` and ``/register_user`` become structured ``login`` / ``register``
    operations; everything else is a ``message``. The raw passcode lives only in
    the local variable below and in the request body — it is never stored,
    logged, or queued (see :func:`redact`).
    """
    ok, reason = configured()
    if not ok:
        logger.warning("Sensei call refused: %s", reason)
        return "Sorry, Sensei is not configured on this installation."

    try:
        user_id = external_user_id(subject)
    except RuntimeError:
        return "Sorry, Sensei is not configured on this installation."

    text = (text or "").strip()

    command = parse_command(text)
    if command:
        return _post({
            "operation": command["operation"],
            "external_user_id": user_id,
            "app_user_id": command["app_user_id"],
            "passcode": command["passcode"],
        })

    # A `/login` or `/register_user` that did not parse is a typo, not chat.
    # Forwarding it would send Sensei a message containing the passcode as free
    # text, and answer the caregiver with something unrelated.
    if is_credential_command(text):
        return command_help()

    return _post({
        "operation": "message",
        "external_user_id": user_id,
        "conversation_id": thread_id or "",
        "message": text,
    })
