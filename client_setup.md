# Client setup

The platform started with older clients and the family member who answers the
phone for them. Some installations look nothing like that: RECO's clients are
young people who register themselves, talk to an agent on their own phone, and
are looked after by an NHS link worker. This page covers the settings that fit
the platform to that, and what each one changes. All of them leave an existing
installation exactly as it was until someone changes them.

| Setting | Where | Default | `.env` |
|---|---|---|---|
| What to call navigators | Settings → General → Clients and wording | "Navigator" (translated) | `NAVIGATOR_TERM`, `NAVIGATOR_TERM_PLURAL` |
| Clients have a caregiver | same card | On | `CAREGIVERS_ENABLED` |
| Ask for a meeting type | same card | On | `MEETING_TYPES_ENABLED` |
| Approve registrations automatically | Settings → Registrations → After someone registers | Off | `SELF_REG_AUTO_APPROVE` |
| Agent for new clients | same card | none | — (DB only: it is a row, not a name) |
| Message while waiting for approval | same card | "…Someone from our team will be in touch with you soon." | `SELF_REG_PENDING_MESSAGE` |
| Welcome message | same card | "Welcome to *brand*! Your registration has been approved…" | `SELF_REG_WELCOME_MESSAGE` |

As everywhere else, the Settings value wins over `.env` (`ConvAI/site_config.py`).

## What a navigator is called

One term, used wherever the role is named to a person: the client list's
column and filter, the client page, the client forms, the approve button, the
Users page and the detail panel. Blank keeps the translated "Navigator". The
plural is the singular plus "s" unless set. The client list used to say
"Counsellor" while the client page said "Navigator"; both now say the same.

The **role** is unchanged: still the `Navigator` group, still "own clients
only" (see [permissions.md](permissions.md)). Only the word moves. Who can be
picked is `roles.navigator_choices()` — active members of that group — on every
picker, so the client form, the approve button and the calendar filter cannot
disagree. An admin is not in it unless they are also added to the group.

## Clients without caregivers

Off, the caregiver fields go from the Add client and Edit client forms, the
Caregiver column from the client list, and the caregiver rows from the client
page. A caregiver already on file is kept and still shown. Calls can still ring
the client's own number, and email reminders already fall back to the client.

**WhatsApp reminders do not.** They are an approved Twilio template that is
addressed to "el cuidador de …" and sent to the caregiver's number, so an
installation without caregivers should send reminders by email (Settings →
Messaging) until that template is replaced.

Independently of the switch, **approving a self-registration no longer creates
a caregiver.** It used to file the person as their own caregiver, which is why
the same name and number appeared twice. Clients approved before this change
keep that duplicate until someone clears it on their Edit page.

## Meeting types

Onboarding / Initial / Protocol / Final / Follow-up come from the first
deployment's programme. Switched off, the meeting dialogs and the detail panel
stop asking, and every new meeting is saved as **Protocol** — the plain
default. Existing meetings keep their type.

## Pronouns

`Patient.pronouns` is free text, entered as the person gave it (suggestions
offered: she/her, he/him, they/them, she/they, he/they). It is on the client
forms and the client page.

Where it is filled in:

* the conversation review (`classify_conversation_with_llm`) is told to use
  them, so the automatic summary does;
* meeting and call-transcript summaries are told too;
* remote agents receive it as `client_pronouns` in their run config — absent,
  not blank, when nobody has recorded them, so a graph can tell the difference.

Blank adds nothing, and the summaries keep writing "they". Nothing infers
pronouns from a name or from a recorded sex. Remote agents cannot write the
field yet; that would be a new run-token scope next to `summary` and
`visibility` ([agent_tools.md](agent_tools.md)).

## After someone self-registers

`ConvAI/registration.py` owns turning a `SelfRegistration` into a client. Both
paths call `registration.approve()`:

* **The Approve button** (Settings → Registrations), with the agent and
  navigator picked on the row. The agent is pre-selected from *Agent for new
  clients*.
* **Automatic approval**, from the registration agent's
  `submit_self_registration` tool, the moment the request is filed. The client
  gets *Agent for new clients* and no navigator. The form refuses to switch it
  on without that agent, because a client with no agent gets no replies.

Either way: no caregiver; the registration is linked to the client
(`SelfRegistration.patient`), so the Registrations table shows **Open client**
and either the navigator's name or an amber **Assign …** link to the client's
Edit page; and approving twice makes one client.

What the person hears:

* While waiting: the registration agent is given *Message while waiting for
  approval* and told to convey it in the person's language. The old prompt
  promised they would "be notified", which nothing guaranteed.
* On approval by an admin: *Welcome message*, sent on the channel they
  registered on (SMS registrations get an SMS; this used to be WhatsApp
  whatever they had used).
* On automatic approval: the agent says the welcome message itself as its
  reply, and nothing else is sent — two messages would cross.

On the admin's Home, a registration shows up twice over its life: while it
waits for approval ("N new registrations waiting to be approved"), and, once it
is a client with nobody assigned, as "N new clients need a *navigator*" — with
automatic approval that is every new client. Both are rows in Alerts to review
and a line at the top of the page, both raise the new-alert toast, and both are
read straight from `SelfRegistration` (`registration.needing_navigator()`), so
assigning a navigator on the client's Edit page is what clears it. Admins only,
and only while self-registration is on. Registrations approved before
`SelfRegistration.patient` existed are not linked to a client and do not appear.

## The calendar

Admins see every meeting in one calendar (navigators see their own clients').
`Meeting.created_by` records who booked each one, and an admin's calendar has a
**Booked by** filter: everyone, one person, or "Not recorded (older meetings)"
for those booked before the field existed. The tooltip on a meeting names who
booked it. The filter holds while moving between days, weeks and months.

A booking from the calendar that fails now says why, field by field. It used to
say only "Please check the meeting details and try again", which made a missing
place for an in-person meeting look like a broken calendar. The calendar and
Communications dialogs now share one save (`calls.save_scheduled_meeting`), so
they agree on clashes; a client with no navigator no longer "clashes" with every
other unassigned client, and a cancelled meeting no longer blocks its slot.

Tests: `ConvAI/test_client_setup.py`.
