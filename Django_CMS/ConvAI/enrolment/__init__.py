"""Study enrolment: pre-enrolled participants, access codes and versioned consent.

A deployment that runs a *study* needs something the platform did not have: a way
to enrol somebody before they ever arrive, a credential they can use to claim
their place, and a record of the consent they gave — including consent to the
conversation classifier and its safety alerts, which this platform has always run
and never asked about.

Off by default (``SiteConfiguration.study_enrolment_enabled``). While it is off
every route here 404s and Settings -> Participants holds only the switch;
switching it off again deletes nothing.

Three modules:

* ``codes``   — generating and normalising the three-word access codes.
* ``service`` — the domain operations: enrol, claim, consent, withdraw, plus the
  attempt limiter that keeps a code from being brute-forced.

The models live in ``ConvAI.models`` (``Study``, ``Enrolment``, ``ConsentRecord``).
See participant_management.md for the whole picture, and for why this is separate
from ``SelfRegistration`` rather than built on it.
"""
from .codes import generate_access_code, normalise_code  # noqa: F401
from .service import (  # noqa: F401
    GENERIC_CODE_ERROR,
    ClaimError,
    approve,
    auto_approve,
    claim,
    clear_attempts,
    code_words,
    create_enrolment,
    enabled,
    link_phone_to_enrolment,
    lookup_claimable,
    note_failed_attempt,
    phone_required,
    record_consent,
    require_dob,
    too_many_attempts,
    withdraw,
)
