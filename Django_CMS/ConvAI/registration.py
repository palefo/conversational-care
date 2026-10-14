"""What happens to a self-registration once it is filed.

The registration agent (``native_agents/self_registration.py``) only collects a
name and a number. Turning that into a client used to live in the approve
button's view, which made it impossible to do any other way — so an
installation that wanted people talking to their agent straight away had to
wait for an admin. It lives here now, and both paths call it:

* **the approve button** on Settings → Registrations, with the agent and
  navigator the admin picks there;
* **auto-approval** (``SELF_REG_AUTO_APPROVE``), straight from the agent's
  tool, with the agent chosen in Settings and no navigator yet.

Either way the person is a client with no caregiver — what they gave is their
own name and number, and filing them as their own caregiver is what put the
same details in both places. See client_setup.md.
"""
import logging

from django.db import transaction
from django.utils.translation import gettext as _

from .site_config import brand_name, get_bool, get_setting

logger = logging.getLogger(__name__)


def default_pending_message() -> str:
    return _("Thank you, your request has been received. Someone from our team will be in touch with you soon.")


def default_welcome_message() -> str:
    brand = (brand_name() or "").strip()
    if brand:
        return _("Welcome to %(brand)s! Your registration has been approved. "
                 "Send a message any time to start.") % {"brand": brand}
    return _("Welcome! Your registration has been approved. Send a message any time to start.")


def pending_message() -> str:
    """What the agent tells someone whose request now waits for an admin."""
    return (get_setting("SELF_REG_PENDING_MESSAGE") or "").strip() or default_pending_message()


def welcome_message() -> str:
    """What someone is told once they are a client."""
    return (get_setting("SELF_REG_WELCOME_MESSAGE") or "").strip() or default_welcome_message()


def auto_approve() -> bool:
    return get_bool("SELF_REG_AUTO_APPROVE")


def client_agent():
    """The agent a newly approved client is given, or None."""
    from .models import Agent, SiteConfiguration

    agent_id = SiteConfiguration.load().self_reg_client_agent_id
    return Agent.for_clients().filter(pk=agent_id).first() if agent_id else None


def notify(sr, body: str) -> bool:
    """Send ``body`` on the channel the person registered on. True if it went."""
    from . import utils

    to = str(sr.phone_number or "")
    if not to or not body:
        return False
    if (sr.details or {}).get("channel") == "sms":
        return utils.send_sms_text(to, body)
    return utils.send_whatsapp_text(to, body)


def approve(sr, *, agent=None, navigator=None, send_welcome=True):
    """Make this registration a client. Returns ``(patient, welcome_sent)``.

    Idempotent: a registration already approved gives back the client it made
    (or None for rows approved before the link existed) and sends nothing.
    """
    from .models import Patient, SelfRegistration

    with transaction.atomic():
        sr = SelfRegistration.objects.select_for_update().get(pk=sr.pk)
        if sr.state == SelfRegistration.State.APPROVED:
            return sr.patient, False
        patient = Patient.objects.create(
            name=sr.name,
            lastname=sr.lastname,
            phone_number=sr.phone_number,
            email=sr.email or "",
            navigator=navigator,
            agent=agent,
        )
        sr.state = SelfRegistration.State.APPROVED
        sr.patient = patient
        sr.save(update_fields=["state", "patient", "updated_at"])

    sent = False
    if send_welcome:
        try:
            sent = notify(sr, welcome_message())
        except Exception:
            logger.exception("Welcome message for registration %s could not be sent", sr.pk)
    return patient, sent


def needing_navigator():
    """Registrations that became clients nobody looks after yet.

    With automatic approval this is every new client until an admin assigns
    one, which is why they are surfaced on Home rather than left on the
    Registrations tab. Each leaves the list the moment a navigator is set.
    """
    from .models import SelfRegistration

    return (SelfRegistration.objects
            .filter(state=SelfRegistration.State.APPROVED,
                    patient__isnull=False, patient__navigator__isnull=True)
            .select_related("patient"))
