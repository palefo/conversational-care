# Link a call recording to the call it came from

Status: **implemented.** Written 2026-08-26 as a proposal from a live diagnosis
against the dev database, and built out the same day; the design below is kept
as the record of why it is shaped this way. Schema in migration
`0080_call_recording_attribution`, backfill in `0081_backfill_recording_owners`,
attribution rules on `CallRecording.for_patient` / `resolve_patient`, covered by
`ConvAI/test_call_attribution.py`. Two related bugs found alongside it were fixed
at the time and are described at the end so this document is not read as
covering them.

## The problem

`CallRecording` has no relation to anything. It stores `from_number` and
`to_number` as free text (`models.py:91`), and every surface that needs to know
whose recording it is re-derives that by string-matching those numbers against
`[patient.phone_number, patient.caregiver.phone_number]`:

- `views/patients.py` — `build_patient_events`, the client timeline and the
  cross-client Communications list
- `views/_panel.py` — `_context_strip` (the "N calls" count), and the meeting
  panel's audio player

A phone number is not an identity. It cannot answer either question the code
asks of it:

**1. Which client?** A number can belong to more than one client. On dev,
`+447920363415` is client Pablo Fonseca's own number *and* the number of the
caregiver assigned to client Hansoo Lee. One call to it is drawn on both
timelines. This is not an edge case — a caregiver looking after two clients,
or a shared household line, produces it in normal use.

**2. A client at all?** `make_phone_conference` (`utils.py:45`) places two
recorded calls per conference — one to the navigator (`CTN`, taken from
`request.user.phone_number` in `views/calls.py:144`) and one to the client side
(`Dyad`), both with `record=True`. The navigator's own leg is a `CallRecording`
whose `to_number` is a *staff* number. Number-matching cannot tell it apart
from a client call, so it is filed against whichever client happens to share
that number.

Observed on dev: one test call produced two recordings and three timeline rows
across three different clients.

## The fix

Stop inferring. The information is known at placement time and thrown away:
`client.calls.create()` returns a call SID for each leg, and `make_phone_conference`
discards both — it has no `return` statement at all, so `views/calls.py` stores
its result into `conference_result` and returns `{"conference": null}` to the
browser on every call.

### Schema

Add to `CallRecording`:

| Field | Type | Purpose |
|---|---|---|
| `meeting` | FK → `Meeting`, null, `SET_NULL` | The call this came from. |
| `patient` | FK → `Patient`, null, `SET_NULL` | Denormalised from `meeting`, so a recording placed outside a meeting still has an owner. |
| `leg` | small int choices: `DYAD` / `CTN` | Which side of the conference. |
| `call_sid` | char, indexed | Twilio's call SID, the join key back to the leg. |

All nullable: 148 existing rows have none of it, and legacy recordings must keep
rendering.

### Code changes

1. `make_phone_conference` returns the two call SIDs (and stops returning `None`).
2. `views/calls.py::start_call` writes a placeholder `CallRecording` per leg — or
   an intermediate `CallLeg` row — carrying `meeting`, `patient`, `leg`, `call_sid`.
3. `get_recordings_from_twilio` (`utils.py:564`) joins incoming recordings on
   `call_sid`, which it already has as `rec.call_sid`, and fills in the relation
   instead of only the numbers.
4. `build_patient_events` and `_panel.py` prefer `patient_id` / `meeting_id`
   when set, and fall back to number-matching only for rows without them.
5. `leg == CTN` recordings are excluded from client timelines. Decide separately
   whether to keep recording that leg at all — the Dyad leg is dual-channel and
   already carries both sides of the conference, so the CTN recording is close
   to a duplicate. `record_ctn=False` may be the whole answer.

### Backfill

148 rows, 5 distinct `to_number` values. A data migration can match on
`(to_number, start_time)` against `Meeting.happened_at` within the existing
90-minute window and set `meeting`/`patient` where exactly one client matches.
Where more than one matches, leave null and let the fallback handle it — do not
guess. Note that 3 `(to_number, start_time)` groups hold more than one row
(same call, both legs, or a repeated Twilio sync); the backfill should not
attach two recordings to one meeting.

### Risks

- Touches the live Twilio path. A failure in leg-recording must not block the
  call from being placed.
- `get_recordings_from_twilio` short-circuits on `date_updated <= last_updated`
  (`utils.py:582`), so a recording that arrives late is skipped permanently.
  Worth revisiting in the same pass — it is why some recordings never appear.

## Fixed separately (already in the tree)

Two bugs that made recordings surface as loose "Call recording" rows. Both are
about *folding*, not attribution, and are done:

- The fold measured its 90-minute window from `Meeting.scheduled_time` while the
  timeline dates and sorts by `happened_at`. A call placed early or retried late
  fell outside the window and orphaned its own audio — on dev, a call booked for
  27 Aug 13:19 and ended 26 Aug 13:24 sat 24.3 h from a recording 26 minutes
  away. Both `build_patient_events` and the meeting panel now anchor on
  `happened_at` via a shared `RECORDING_MATCH_WINDOW` in `views/_panel.py`.
- The fold was greedy: it picked the single nearest meeting and, if that one was
  already taken, orphaned the recording rather than trying the next. With two
  recordings per conference this fired constantly. It now walks nearest-free
  first, and skips `CANCELLED` meetings.
