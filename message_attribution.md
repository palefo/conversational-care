# Whose a message is

Status: **implemented.** Schema in migration `0089_message_owner_and_run_callbacks`,
backfill in `0090_backfill_message_owners`, the rule in
`ConvAI/message_attribution.py`, covered by `ConvAI/test_message_attribution.py`.
The same move call recordings made in `0080`/`0081`; see
[call_recording_attribution.md](call_recording_attribution.md).

## The problem

`Message` had no relation to anybody. It stored the sender as a free-text `user`
field — a phone number for WhatsApp and SMS, a username for the tester chat and
the Link Worker bubble, an email or the literal `"api-user"` for the API — and
every screen that needed to know whose a message was re-derived it by matching
that string against clients' **current** phone numbers:

| Where | What it decided |
|---|---|
| `patient_message_q` | Every client timeline, the day panel, the history page, alerts, the conversation download |
| `_panel.py` context strip | "N chat days", "last contact" — by number only |
| `_panel.py` alert panel | The exchange behind an alert |
| `dashboard.py` | A navigator's message counts |
| `views/media.py` | **Who may download a voice note** |
| `views/feedback.py` | **Who may rate a message** |
| `conversation_alerts.py` | Which agent's detectors apply, which client an alert is about |

A phone number is an address, not an identity. Matching it at read time means:

- **A client who changes number loses their history** from every screen.
- **A stranger given a client's old number inherits it** — and the last two rows
  above are authorisation checks, so their navigator could download the old
  client's voice notes.
- **A caregiver who changes number** stops being named as the one who wrote.

And on receipt, four inbound paths each ran their own
`Patient.objects.filter(phone | caregiver phone).first()` — unordered, so a
number shared by two clients went to whichever the database returned first.

## The fix

**A number is used once: when the message arrives.** The answer is stamped on
the message and never re-derived.

### Schema

| Field | What it holds |
|---|---|
| `Message.patient` | The client file it belongs to. Nullable, `SET_NULL`. |
| `Message.account` | The login it came through, when it came through one. |
| `Message.sender_role` | Who wrote in: `client`, `caregiver`, `staff`, `tester`, `api`, `prospect`, `platform`. Blank only for legacy rows. |
| `Message.user` | Unchanged: the raw address, kept as history. |

Two owner fields because messages come from two kinds of source, mirroring
`Conversation.patient` / `Conversation.user`:

| Path | patient | account | role |
|---|---|---|---|
| WhatsApp / SMS text and audio | resolved | — | client / caregiver |
| Tester web chat (text and voice) | the linked client | the tester | tester |
| Link Worker bubble | — | the navigator | staff |
| `/api/v1/messages/` (SDK, RECO today) | the conversation's, if any | the API account | api |
| Self-registration | — | — | prospect |
| Reminders, alert SMS, care plans, protocol texts | the client | — | platform |

Every write goes through `message_attribution.create_message`, whose
`sender_role` has no default: a new write path has to say who wrote in.

### On receipt: one resolver

`resolve_inbound(number)`, used by WhatsApp text, WhatsApp audio (sync and
async) and SMS:

1. A client's own number beats a caregiver number.
2. Among several clients still, the one this number most recently wrote about.
3. Failing that, the oldest client record.

A shared number is not an error — households share a phone, a caregiver can look
after two people — but it is where the answer is a rule rather than a fact. Every
ambiguous match is logged, and **Settings → Messaging → Shared numbers** lists
them with links to each client.

### When a number changes

Nothing to do: the history is on the client by FK. The **old number stops
resolving at once** — a message from it goes to the unknown-sender path like any
stranger's. Numbers get recycled, and a stranger's words landing on a client's
record, to be read by their link worker, is worse than one missed message.

### Reads

Every read in the table above now goes through the owner fields:
`patient_messages_q(patient)`, `navigator_messages_q(user)`, and `msg.patient` /
`msg.account` for the two authorisation checks. The panel's "Written by" reads
`sender_role`.

## Existing messages

`0090` places each existing message using only evidence that does not move when
a number does, in this order: the conversation it is in; the key it is filed
under (`reminder-<meeting>`, `careplan-<client>`, `alert-<alert>`); the login it
names; and — **only when the number belongs to exactly one client** — its
number. It prints what it did.

What it cannot place stays blank (`legacy_q()`), and **those rows alone** are
still matched by number when read. Settings → Messaging shows how many there
are; when it reaches zero on an installation, the fallback in
`patient_messages_q` can be deleted.

## Known limits

- `sender_role` says *caregiver*, not *which* caregiver. If a client's caregiver
  is replaced by a different person, older messages are labelled with the
  current one's name. A `Caregiver` FK on the message would fix that if it
  matters.
- Call recordings keep their own attribution (`CallRecording.patient`,
  `for_patient`), which already worked this way.
