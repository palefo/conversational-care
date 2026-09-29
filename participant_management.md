# Participant management

Some deployments of Conversational Care run a **research study**. That needs
three things the platform did not have: a way to enrol somebody *before* they
ever make contact, a credential they can use to claim their place, and a record
of the consent they gave.

**Off by default, and invisible while off.** Running a study is what a few
installations do, not what the platform is for. The switch lives in
**Settings → Participants** (`SiteConfiguration.study_enrolment_enabled`, or
`STUDY_ENROLMENT_ENABLED` in `.env`) and starts blank.

While it is off, an installation cannot tell this feature was ever added —
apart from the switch itself:

* **Settings → Participants holds the switch and nothing else.** No options, no
  studies, no join link, and none of their queries run. The switch has to be
  somewhere an admin can reach it: `SiteConfiguration` is not in the Django
  admin, so hiding the whole tab would leave `.env` as the only way to turn it
  on — and an admin who switched it *Off* there could never switch it back. The
  options travel along as hidden inputs, so flipping the switch never resets
  them;
* no enrolment block on the Clients page, and no study section on a client's page;
* no **Studies**, **Enrolments** or **Consent records** in the Django admin —
  their pages refuse even by direct URL — and no *study* field on the user form;
* every enrolment route 404s, including the REST endpoint, and the public join
  page does not exist;
* the self-registration agent gets neither the access-code tool nor the wording
  about codes;
* `_participants_context` returns immediately, so the settings page costs what it
  did before.

`ConvAI/test_enrolment.py::OffLooksUntouchedTests` asserts each of those, and
`ConvAI/test_enrolment_teeth.py` asserts the mirror image — that with the switch
**on** the very strings those tests require to be absent are present — so none of
them can pass vacuously.

Turning it off again deletes **nothing**: the enrolments and the consent records
people signed are kept, and turning it back on costs an admin nothing.

> The switch is read through `SiteConfiguration`, which is cached for 30 seconds
> per process, so flipping it can take up to half a minute to show everywhere.

## Why this is not `SelfRegistration`

The platform already had a way for a stranger to ask to be let in: they message
the service, the self-registration agent collects their details, and an admin
approves them (`native_agents/self_registration.py`). It is tempting to hang a
study on that model. It does not fit, because the two run in opposite directions.

| | `SelfRegistration` | `Enrolment` |
|---|---|---|
| Starts from | an unknown inbound number | a clinician who already knows the person |
| Keyed on | `phone_number` | an access code; no phone exists yet |
| Lifetime | **consumed at approval** — no link back to the client it created | outlives approval, for as long as the study runs |
| States | `REGISTERED` → `APPROVED` | invited → consented → active → completed / withdrawn |

Three concrete breakages if the two were merged:

1. `views/dashboard.py` counts every `REGISTERED` row into the admin's alert
   badge. Two hundred pre-enrolled participants who have not claimed yet would be
   a badge of two hundred and a queue nobody could clear.
2. The agent dedupes on `phone_number`. Pre-enrolled rows have no phone, so they
   would collapse into one another.
3. `approve_self_registration` consumes the row. The study arm and the consent
   history would be stranded on something the approval path treats as spent.

So they stay separate — and are joined at one point instead. See
[the bridge](#the-bridge-to-self-registration) below.

## The models

| Model | What it holds |
|---|---|
| `Study` | One study, or one arm of one: its consent wording and version, its information sheet, its questionnaires, and the agent its participants talk to. |
| `Enrolment` | One person's place in a study: their access code, status, and (once claimed) a link to their `Patient`. |
| `ConsentRecord` | What one person agreed to, when, and **in which words**. Append-only. |

### A claim is not an admission

The single most important ordering in this feature: a participant **claims**
their code first and **consents** second, and the `Patient` is created at the
*second* step.

Somebody who types a valid code and then closes the consent page has agreed to
nothing. Creating a client record for them would put an unconsented person into
the clinical side of the platform, where navigators would start working with
them. So a claim only fills in who they are; consent is what admits them.

### A participant *is* a client

Once somebody consents they are a `Patient` like any other, and `Enrolment.patient`
points at them. There is deliberately **no separate Participants page**: a second
list would have shown most of the cohort twice, and made a link worker guess which
page to open.

So enrolment lives where the people do:

| Where | What it holds |
|---|---|
| **Clients page** | The enrol form, and the *waiting to arrive* list — people issued a code who have not claimed it. Once they consent they drop off it and appear in the client table below. |
| **A client's page** | Their study, status, access code and full consent history, under *Study*. |
| **Settings → Participants** | The switch, the enrolment options, and the studies with their consent wording. |

The one page of its own is the enrolment detail (`/participants/<id>/`), which is
where a participant with no client record yet can still be opened, and where
withdrawal lives.

### Status is mostly recorded, not chosen

*Invited* and *consented* are facts: a consent record exists or it does not. So
they are set by the platform, never picked from a list — otherwise the list could
say somebody consented when nobody has. What staff set by hand is the one thing
only they know: that a participant has **completed** the study (or, if that was
a mistake, is **active** again). That choice is only offered for somebody with a
client record. Withdrawal is its own action, because it has side effects.

### Every study needs an agent

A participant is given their study's agent when they consent. A study with none
gives them a client record whose messages are answered *"no agent is
configured"*, so Settings flags any study without one, and the editor's empty
choice says what it means.

### Consent is evidence, so it is append-only

`ConsentRecord` refuses to be re-saved; the model raises on any write to an
existing row. Re-consenting to a new version **inserts** a row.

That is the whole point of the model. Consent kept as a few booleans on the
participant means a reworded form quietly becomes the thing everybody has
supposedly agreed to, and there is no way to show what any individual actually
read. So each record also snapshots the wording (`items_text`) beside the answers
(`items`), and keeps the version, the time, and the address it came from.

An item that was shown and **declined** is recorded as `False`, not omitted —
"they said no" and "we never asked" are different facts.

### Consent locks the way a protocol locks

A protocol stays editable until answers are recorded against it, after which it
locks to protect collected data. Consent wording follows the same rule: it is
editable until somebody consents to that version, after which the editor refuses
to change it.

Editing from then on means **raising `consent_version`**, which starts a fresh
version and leaves the signed records pointing at the text those participants
read. Settings then shows how many people are on an older version — they are
asked again on their next visit (`Enrolment.consent_is_current`).

The rule is applied to the version **being saved**, not the one on record:

* Change the version number and the tick-boxes unlock in the editor straight
  away, so the new version's wording is written and saved in one go.
* Keep the signed version and the wording is fixed — but everything else about
  the study (its name, whether it is open, its agent) still saves normally.
* A version somebody already signed cannot be reused for different words: going
  back from 2.0 to 1.0 is refused rather than rewriting what 1.0 meant.

Re-consenting to a new version adds a record without touching the participant's
status, so somebody active stays active.

### The consent wording is data, not markup

`Study.consent_items` is a list of `{key, text, required}`. The tick-boxes on the
consent page are rendered from it, and `ConsentRecord.items` is keyed by the same
keys. A study team can correct a sentence in **Settings → Participants → the
study** without waiting for a deploy.

A new study starts from `models.DEFAULT_CONSENT_ITEMS`, whose third item is the
one that matters most here:

> I understand that automated methods may review my conversations for content
> suggesting severe distress, self-harm, suicidality or safeguarding concerns…

The platform has always run a classifier over every inbound message and raised
safety alerts from it (see [agents.md](agents.md)). Until this feature there was
nothing recording that anybody had agreed to that.

## Access codes

Three words, hyphenated: `maple-crane-frost`. Words rather than a UUID because
the code is read down a phone line, written on a card, and typed by somebody who
is not enjoying the experience — `maple-crane-frost` survives all three.

Generated with `secrets.choice`, never `random`: a code is the only thing between
a stranger and somebody's place in a study, which makes it a credential. The
unique constraint on the column is the real collision guard; the retry loop is a
courtesy.

`normalise_code` accepts what people actually type — capitals, spaces instead of
hyphens, a trailing full stop, a smart dash — because none of that is a wrong
code.

**The attempt limit is the real defence**, not the size of the word list. Three
words from ~250 is roughly 2²³ orderings: plenty against typing, not much against
a script. `ENROLMENT_CODE_ATTEMPT_LIMIT` (default 10 per address per hour) is what
makes guessing impractical.

## The public join page

`/join/` → `/join/claim/` → `/join/consent/` → `/join/done/`, plus
`/join/resume/`. These are the **only unauthenticated pages in the platform**,
which is what most of the care in `views/join.py` is about.

* The whole flow 404s while the feature is off.
* **CSRF stays on.** (The implementation this replaces marked its endpoints
  `@csrf_exempt`; nothing here needs that.)
* **One message for every failure.** "No such code", "already claimed" and "that
  study has closed" are answered identically, and every miss is counted against
  the caller. Otherwise the form is an oracle for guessing codes and confirming
  who is in a study.
* **The form's questions never depend on the code.** Everything answerable
  without it — is a date of birth required, is a phone number required — is
  answered *before* the lookup. Asking "we also need your date of birth" only
  once a code has matched would leak just as loudly as a different error
  message, and for free: a guessed code answered with a follow-up question is a
  code that exists. This is why `require_phone` is per study but
  `service.phone_required()` asks whether **any open study** needs one; over-
  asking in a deployment whose studies disagree is the cheaper mistake.
* **The name is never taken from the form.** The clinician entered it. Letting
  the claim form overwrite it would let somebody with a leaked code rewrite whose
  enrolment it is.
* The session key is cycled on a successful claim — that is the moment the
  session starts representing a specific person.
* **Consent comes after a claim, never instead of one.** The consent page sends
  anybody whose code is unclaimed back to the claim form, and `/join/resume/`
  gives an unclaimed code no session at all — otherwise resuming would be a way
  round the date of birth and phone number the claim asks for.
* Somebody whose consent is already current is sent to the confirmation page
  rather than shown the form again, so reloading cannot write a second record.

The information sheet **gates the tick-boxes**: they stay disabled until the PIS
link has been opened. It is a prompt, not proof — see the limitations.

## Configuration

All of it resolves DB-first (**Settings → Participants**) then `.env`, like every
other runtime setting.

| Setting | Default | What it does |
|---|---|---|
| `STUDY_ENROLMENT_ENABLED` | off | The switch. |
| `ENROLMENT_AUTO_APPROVE` | on | Whether a valid code plus consent creates the client straight away, or files it for an admin. On, because issuing the code *was* the vouching. |
| `ENROLMENT_REQUIRE_DOB` | off | Whether the claim form asks for a date of birth. Off: a study that does not need it should not hold it. |
| `ENROLMENT_CODE_WORDS` | 3 | Words per access code (2–4). |
| `ENROLMENT_CODE_ATTEMPT_LIMIT` | 10 | Wrong codes per address per hour before a lockout. |
| `ENROLMENT_LANDING_TEXT` | — | The wording on the public join page. |

Per **study**, in its editor: name, slug, open/closed, PIS URL, consent version,
intro and tick-boxes, post-consent and baseline questionnaire URLs, whether a
phone number is required, the default agent, chief investigator and IRAS ID.

## Study-scoped staff

`ConvAIUser.study` narrows the enrolment block on the Clients page to one study. Blank sees all of
them, which is right for an installation running one study or none, and admins
ignore it. It is never a permission on its own — it narrows what an already
authorised member of staff sees, and it is enforced on the detail page as well as
the list, so a scoped navigator cannot reach another arm's participant by
guessing a URL.

## The bridge to self-registration

Where enrolment is on, the self-registration agent gains one tool,
`check_access_code`. If an unknown number messages the service and gives a valid
code, a clinician has already vouched for that person, so there is nothing for an
admin to approve — filing a registration request would be asking somebody to
confirm a decision already made.

It deliberately stops short of creating the `Patient`: **sending a code over
WhatsApp is not consent.** The agent links the phone number to the enrolment,
greets them by name, and sends them to the join page, where consent is taken.

Because a match hands back a name, codes sent by message count against the same
attempt limit as the web form, per sending number. Without it anyone could text
the service and let the agent guess for as long as they liked.

The join link is built from `AGENT_CALLBACK_URL`, which already holds this
platform's public base URL. Blank there simply means no link is offered and the
agent registers the person normally.

## The API

`GET /api/v1/enrolments/?phone=…` or `?code=…`, behind the same API token as
the rest of the API, 404ing while the feature is off. It answers whether somebody
is enrolled, which study, their status and their consent state.

It **never returns the access code**. A token that can look participants up must
not thereby become a token that can impersonate them. (The endpoint this replaces
was unauthenticated and CSRF-exempt, and returned a participant's name for any
phone number posted to it.)

## Withdrawal

Withdrawing sets the status, records the time and reason, and **switches the
client's agent off** — so it actually stops the messages rather than only
annotating that it should have.

It deletes nothing. A withdrawal is part of a study's record, and the
conversations already held still happened.

The Django admin agrees: a consent record cannot be deleted, and neither can an
enrolment that has one — deleting it would take its consent records with it.
An enrolment nobody consented to (a typo, say) can still be removed.

## Limitations

Worth stating plainly, because a study team will be asked about all of them.

* **This records that somebody agreed. It is not a qualified e-consent system** —
  no signature, no witness step, no sponsor approval workflow, and it is not an
  ethics-approved eCRF. Sponsor sign-off is still needed.
* **The PIS gate is a prompt, not proof.** It records that the link was opened,
  which is not the same as the sheet having been read.
* **Access codes are single plaintext credentials.** No second factor. Their
  protection is the attempt limit.
* **A study that requires a phone number makes every participant give one**,
  including participants of a study that does not, where the two run on one
  installation. That is the cost of not letting the question leak whether a code
  exists.
* **The attempt limit is per process** unless the deployment configures a shared
  cache, and the caller's address is taken from `X-Forwarded-For`, which is
  spoofable behind a misconfigured proxy. It raises the cost of guessing; it does
  not make it impossible.
* **Questionnaire completion is self-reported.** The post-consent and baseline
  URLs are redirects; nothing posts back.
* **Nobody is randomised.** Whoever enrols the participant picks the arm.
* **Clinician actions are audited only as far as Django admin history goes.**
* **The new interface strings are not yet translated**, so they fall back to
  English on a platform running in another language.

## Tests

`ConvAI/test_enrolment.py` (110 tests) and `ConvAI/test_enrolment_teeth.py`
(8 tests, the anti-vacuity mirror of the off-state ones). `NoTemplateLeakTests`
also renders every page the feature adds and fails if template syntax reaches
the screen — Django's `{# … #}` is single-line only, and one spread over two
lines prints verbatim:

```bash
python3 manage.py test ConvAI.test_enrolment ConvAI.test_enrolment_teeth --settings=test_settings
```
