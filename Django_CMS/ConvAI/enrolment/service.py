"""The domain operations behind study enrolment.

Ordering matters here, and it is the one thing worth reading before the code.

A participant **claims** their code first and **consents** second, and the
``Patient`` is created at the *second* step, not the first. Somebody who types a
valid code and then closes the consent page has agreed to nothing, and creating a
client record for them would put an unconsented person into the clinical side of
the platform where navigators would start working with them. So a claim only
fills in who they are; consent is what admits them.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from ..site_config import get_bool, get_int
from .codes import generate_access_code, normalise_code

if TYPE_CHECKING:  # the models are imported inside each function at runtime
    from ..models import ConsentRecord, Enrolment, Patient

# Deliberately one message for "no such code", "already claimed" and "study
# closed". Telling a stranger which of those it was turns the form into an oracle
# that confirms a code exists, or that a named study is running.
GENERIC_CODE_ERROR = _(
    "We could not match that access code. Please check it and try again, or "
    "contact the person who gave it to you."
)

_ATTEMPT_WINDOW = 60 * 60  # one hour, matching the setting's wording


class ClaimError(Exception):
    """A claim that cannot proceed, carrying text safe to show a stranger."""


# ----------------------------
# Switches
# ----------------------------

def enabled() -> bool:
    return get_bool("STUDY_ENROLMENT_ENABLED")


def auto_approve() -> bool:
    # Default on: issuing the code was the vouching. See the model comment.
    return get_bool("ENROLMENT_AUTO_APPROVE", default=True)


def require_dob() -> bool:
    return get_bool("ENROLMENT_REQUIRE_DOB", default=False)


def phone_required() -> bool:
    """Whether the join form must ask for a phone number.

    ``Study.require_phone`` is per study, but the question has to be asked before
    a code is looked up — otherwise "we also need your number" is only ever said
    about a code that exists, which tells a stranger their guess was right. So it
    is asked whenever *any* open study needs one. Over-asking in a deployment
    whose studies disagree is the cheaper mistake.
    """
    from ..models import Study

    return Study.objects.filter(is_open=True, require_phone=True).exists()


def code_words() -> int:
    return get_int("ENROLMENT_CODE_WORDS", default=3)


def attempt_limit() -> int:
    return get_int("ENROLMENT_CODE_ATTEMPT_LIMIT", default=10)


# ----------------------------
# Attempt limiting
# ----------------------------
# The real defence for a guessable credential. Keyed on the caller's address and
# counted in the cache: this is per-process unless the deployment configures a
# shared cache, which is stated as a limitation rather than pretended away.

def _attempt_key(ip: str) -> str:
    # ``ip`` is whoever is guessing: an address for the web form, "phone:<number>"
    # for a code sent by message (see link_phone_to_enrolment).
    return f"enrolment:attempts:{ip or 'unknown'}"


def too_many_attempts(ip: str) -> bool:
    return int(cache.get(_attempt_key(ip)) or 0) >= attempt_limit()


def note_failed_attempt(ip: str) -> None:
    key = _attempt_key(ip)
    try:
        # add() then incr() rather than get/set: two requests arriving together
        # should count as two attempts, not one.
        cache.add(key, 0, _ATTEMPT_WINDOW)
        cache.incr(key)
    except ValueError:
        # The key expired between add and incr. Start the window again.
        cache.set(key, 1, _ATTEMPT_WINDOW)


def clear_attempts(ip: str) -> None:
    """Called on a successful claim, so one mistyped code does not haunt someone."""
    cache.delete(_attempt_key(ip))


# ----------------------------
# Enrolling (clinician side)
# ----------------------------

def create_enrolment(*, study, name: str, lastname: str, created_by=None,
                     phone_number=None, access_code: str = "") -> "Enrolment":
    """Pre-enrol a participant and issue their code.

    Retries on the unique constraint rather than trusting a pre-check, because the
    only race-free guarantee of uniqueness is the constraint itself.
    """
    from ..models import Enrolment

    name = (name or "").strip()
    lastname = (lastname or "").strip()
    if not name or not lastname:
        raise ValueError("A first name and a last name are both required.")
    if not study.is_open:
        raise ValueError("That study is closed to new enrolments.")

    supplied = normalise_code(access_code)
    for attempt in range(6):
        code = supplied or generate_access_code(
            code_words(),
            exclude=lambda c: Enrolment.objects.filter(access_code=c).exists(),
        )
        try:
            with transaction.atomic():
                return Enrolment.objects.create(
                    study=study,
                    name=name,
                    lastname=lastname,
                    phone_number=phone_number or None,
                    access_code=code,
                    created_by=created_by,
                )
        except IntegrityError:
            if supplied:
                # They typed this one themselves, so say so instead of silently
                # handing back a different code.
                raise ValueError(f'The code "{supplied}" is already in use.')
            continue
    raise RuntimeError("Could not issue an unused access code after several attempts.")


# ----------------------------
# Claiming (participant side)
# ----------------------------

def lookup_claimable(code: str):
    """Return the enrolment this code can still claim, or None.

    None covers every reason equally — unknown code, already claimed, withdrawn,
    study closed — because the caller must not tell them apart.
    """
    from ..models import Enrolment

    code = normalise_code(code)
    if not code:
        return None
    enrolment = Enrolment.objects.select_related("study").filter(access_code=code).first()
    if enrolment is None:
        return None
    if enrolment.claimed_at is not None:
        return None
    if enrolment.status == Enrolment.Status.WITHDRAWN:
        return None
    if not enrolment.study.is_open:
        return None
    return enrolment


def claim(*, code: str, date_of_birth=None, phone_number=None, ip: str = "") -> "Enrolment":
    """Bind a code to the person in front of us. Does not create a ``Patient``.

    The name is not taken from this form on purpose: the clinician already entered
    it, and letting the form overwrite it would mean a stranger with a leaked code
    could rewrite whose enrolment it is.
    """
    if too_many_attempts(ip):
        raise ClaimError(_(
            "Too many incorrect attempts. Please wait an hour and try again, or "
            "contact the person who gave you the code."
        ))

    # Everything that can be answered *without* the code is answered first, and
    # deliberately so. Asking "what else does this form need?" only after a code
    # matched would make those messages an oracle: a guessed code that came back
    # "please enter your date of birth" is a code that exists, told apart from a
    # wrong one for free and without even spending an attempt. So the questions
    # the form asks never depend on which code was typed.
    if require_dob() and not date_of_birth:
        raise ClaimError(_("Please enter your date of birth."))
    if phone_required() and not phone_number:
        raise ClaimError(_("A mobile number is needed so the service can message you."))

    enrolment = lookup_claimable(code)
    if enrolment is None:
        note_failed_attempt(ip)
        raise ClaimError(GENERIC_CODE_ERROR)

    enrolment.date_of_birth = date_of_birth or enrolment.date_of_birth
    if phone_number:
        enrolment.phone_number = phone_number
    enrolment.claimed_at = timezone.now()
    enrolment.save(update_fields=["date_of_birth", "phone_number", "claimed_at", "updated_at"])

    clear_attempts(ip)
    return enrolment


# ----------------------------
# Consent
# ----------------------------

def record_consent(*, enrolment, answers: dict, ip=None, user_agent: str = "") -> "ConsentRecord":
    """Write an append-only consent record and, if complete, admit the participant.

    ``answers`` is ``{item_key: bool}`` for every tick-box that was shown. Every
    item the study marks required has to be True — a partially agreed consent is
    not a consent, and is refused rather than stored as one.
    """
    from ..models import ConsentRecord, Enrolment, Study

    # Re-read the study rather than trust the relation cached on the enrolment.
    # A consent record is evidence of what was on the page at this moment, so the
    # version and wording it stores have to come from the row as it is now — not
    # from a copy loaded before somebody in Settings raised the version.
    study = Study.objects.get(pk=enrolment.study_id)
    items = study.consent_items or []
    missing = [i["key"] for i in items if i.get("required") and not answers.get(i["key"])]
    if missing:
        raise ClaimError(_("Please agree to each required item before continuing."))

    with transaction.atomic():
        record = ConsentRecord.objects.create(
            enrolment=enrolment,
            consent_version=study.consent_version,
            # Recorded for every item shown, so an optional item that was declined
            # reads as declined rather than as absent.
            items={i["key"]: bool(answers.get(i["key"])) for i in items},
            items_text={i["key"]: i.get("text", "") for i in items},
            ip_address=ip or None,
            user_agent=(user_agent or "")[:400],
        )

        # Only a first consent moves the status. Re-consenting to a new version
        # is somebody already taking part agreeing again; it must not demote an
        # active (or completed) participant back to "consented".
        if enrolment.status == Enrolment.Status.INVITED:
            enrolment.status = Enrolment.Status.CONSENTED
            enrolment.save(update_fields=["status", "updated_at"])

        if auto_approve() and enrolment.patient_id is None:
            _create_patient_for(enrolment)

    return record


def _create_patient_for(enrolment) -> "Patient":
    """Create the clinical record for a consented participant.

    Mirrors ``settings_views.approve_self_registration``: a Caregiver alongside the
    Patient, so the existing reminder and contact paths have somewhere to write.
    The agent comes from the study, which is how an arm gets the right assistant
    without anybody setting it per person.
    """
    from ..models import Caregiver, Enrolment, Patient

    patient = Patient.objects.create(
        name=enrolment.name,
        lastname=enrolment.lastname,
        phone_number=enrolment.phone_number,
        agent=enrolment.study.default_agent,
        caregiver=Caregiver.objects.create(
            name=enrolment.name,
            lastname=enrolment.lastname,
            phone_number=enrolment.phone_number,
        ),
    )
    enrolment.patient = patient
    enrolment.status = Enrolment.Status.ACTIVE
    enrolment.save(update_fields=["patient", "status", "updated_at"])
    return patient


def link_phone_to_enrolment(code: str, phone: str) -> dict:
    """Match a code somebody sent by message, and remember the number they sent it from.

    The bridge between this and ``SelfRegistration``. When an unknown number
    messages the service and gives a valid access code, a clinician has already
    vouched for that person, so there is nothing for an admin to approve — filing
    a registration request would be asking somebody to confirm a decision that was
    already made.

    It deliberately stops short of creating the ``Patient``. Sending a code over
    WhatsApp is not consent, and consent is what admits somebody. So this links
    the number and hands them to the join page; the web flow does the rest.
    """
    if not enabled():
        return {"ok": False, "reason": "disabled"}

    # The same attempt limit as the web form, counted per sending number. A
    # match hands back a name, so without it anyone could text the service and
    # let the agent try codes for as long as they liked.
    phone = (phone or "").strip()
    caller = f"phone:{phone}"
    if too_many_attempts(caller):
        return {"ok": False, "reason": "locked"}

    enrolment = lookup_claimable(code)
    if enrolment is None:
        note_failed_attempt(caller)
        return {"ok": False, "reason": "no-match"}
    clear_attempts(caller)

    if phone and not enrolment.phone_number:
        enrolment.phone_number = phone
        enrolment.save(update_fields=["phone_number", "updated_at"])

    return {
        "ok": True,
        "name": enrolment.name,
        "study": enrolment.study.display_name,
        "needs_consent": enrolment.latest_consent is None,
    }


def approve(enrolment) -> "Patient":
    """Admit a consented participant by hand, where auto-approval is off."""
    from ..models import Enrolment

    if enrolment.patient_id is not None:
        return enrolment.patient
    if enrolment.status not in (Enrolment.Status.CONSENTED, Enrolment.Status.ACTIVE):
        raise ValueError("Only a participant who has consented can be admitted.")
    return _create_patient_for(enrolment)


# ----------------------------
# Withdrawal
# ----------------------------

def withdraw(enrolment, *, reason: str = "", by=None):
    """Mark somebody as withdrawn and stop the platform contacting them.

    Deliberately not a delete. A withdrawal is part of a study's record, and the
    conversations already held still happened. What it does do is switch the
    agent off for their client record, so withdrawing actually stops the messages
    rather than only annotating that it should have.
    """
    from ..models import Enrolment

    enrolment.status = Enrolment.Status.WITHDRAWN
    enrolment.withdrawn_at = timezone.now()
    enrolment.withdrawn_reason = (reason or "")[:300]
    enrolment.save(update_fields=["status", "withdrawn_at", "withdrawn_reason", "updated_at"])

    patient = enrolment.patient
    if patient is not None and patient.chatbot_enabled:
        patient.chatbot_enabled = False
        patient.chatbot_off_reason = "Withdrawn from study"
        patient.chatbot_off_at = timezone.now()
        patient.chatbot_off_by = by
        patient.save(update_fields=[
            "chatbot_enabled", "chatbot_off_reason", "chatbot_off_at", "chatbot_off_by",
        ])
    return enrolment
