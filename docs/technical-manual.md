# Conversational Care — Technical Manual

This manual is the developer- and operator-facing reference for the
Conversational Care platform. It consolidates and extends the topic guides in
the repository root ([install_guide.md](../install_guide.md),
[database.md](../database.md), [permissions.md](../permissions.md),
[agents.md](../agents.md), [async_replies.md](../async_replies.md),
[file_storage.md](../file_storage.md)) — those stay the authoritative deep
dives; this document ties them together and fills the gaps (architecture,
configuration reference, REST API, i18n, deployment checklist).

A non-technical, task-oriented guide for care teams lives in the
[User Manual](user-manual/index.html) (static HTML, publishable as-is).

---

## Table of contents

1. [What is Conversational Care?](#1-what-is-conversational-care)
2. [Architecture overview](#2-architecture-overview)
3. [The web UI: detail panel & Communications](#3-the-web-ui-detail-panel--communications)
4. [Project layout](#4-project-layout)
5. [Installation](#5-installation)
6. [Configuration reference](#6-configuration-reference)
7. [Database](#7-database)
8. [Users, roles & permissions](#8-users-roles--permissions)
9. [Agents](#9-agents)
10. [Messaging: Twilio webhook & async replies](#10-messaging-twilio-webhook--async-replies)
11. [File storage & media security](#11-file-storage--media-security)
12. [Internationalisation](#12-internationalisation)
13. [REST API & Python client](#13-rest-api--python-client)
14. [Security & deployment checklist](#14-security--deployment-checklist)
15. [Development workflow](#15-development-workflow)

---

## 1. What is Conversational Care?

**Conversational Care** is a customisable platform that integrates AI agents
into care-team workflows, automating routine tasks and escalating to humans
when oversight is needed. It was built through a two-year Research through
Design process and deployed across the UK, Peru, and Singapore.

The platform gives a **care team** (admins and *navigators* — care team
navigators, CTNs) a web application to manage **clients** (people receiving
care and their caregivers), schedule and run **structured phone calls**
(driven by *protocols* — question sets designed in a form editor), monitor
**chat conversations** between clients and AI **agents** (via WhatsApp/SMS or
an embedded web chat with voice), and review **alerts** raised when a
conversation needs human attention.

## 2. Architecture overview

```
                      ┌────────────────────────────────────────────┐
    Care team         │   Django app (ConvAI)                      │
    browser  ────────▶│                                            │
                      │  • Web UI (Tailwind, server-rendered)      │
    Twilio            │  • REST API (DRF, token auth, /api/v1/…)   │──▶ PostgreSQL
    WhatsApp/SMS ────▶│  • Twilio webhook (+ async worker pool)    │    (app data +
    webhook           │  • Native agents (in-process LangGraph)    │    LangGraph
                      │  • Media views (signed, ownership-checked) │    checkpoints)
                      └──────┬──────────────┬──────────────┬───────┘
                             │              │              │
                       LLM providers   ElevenLabs      Remote LangGraph
                       (OpenAI, Anthropic, (TTS)       agents (host:port,
                       Google, Mistral,                 SSRF allow-listed)
                       DeepSeek — or Azure)
```

Key design decisions:

- **One deployable unit.** The web app, the REST API, the webhook handler, the
  background reply workers, and the in-process agents all run inside the same
  Django process — no Redis, no Celery, no separate worker deployment
  (see [§10](#10-messaging-twilio-webhook--async-replies)).
- **Two agent execution models.** Agents either run *in-process* (native and
  prompt-based kinds, persisted via a LangGraph Postgres checkpointer) or
  *remotely* on a LangGraph server you operate (see [§9](#9-agents)).
- **Runtime configuration.** Most integration settings can be changed at
  runtime in **Settings** (stored on a `SiteConfiguration` singleton) and fall
  back to `.env`. Resolution order: **DB override → environment → Django
  settings → default** (`ConvAI/site_config.py`).
- **Private-by-default media.** No private file has a public URL; access goes
  through ownership-checked views or single-use signed tokens
  (see [§11](#11-file-storage--media-security)).

## 3. The web UI: detail panel & Communications

Reading or working any single item — an alert, a chatbot conversation, a
scheduled call, a finished call, an in-person meeting, a recording — happens in
a **right-hand detail panel** that opens in place. The separate pages that used
to do this are retired.

### How the panel works

The open item lives in the URL as a query parameter:

```
?item=<kind>-<id>          alert-12 · meeting-983 · chat-96-2026-08-04
```

`ConvAI/views/_panel.py` resolves that token (`resolve_panel_item`) and
`ConvAI/templates/_detail_panel.html`, rendered from `base.html`, draws it.
Because the state is a URL parameter and not client-side state, a panel can be
linked, refreshed, opened in a new tab, and survives navigation between pages.
`resolve_panel_item` returns `None` for anything the current user may not read,
so a stale or guessed link degrades to a plain page rather than leaking whether
the item exists.

A chat item is a *patient plus a day*, not a single row — hence the
`chat-<patient_pk>-<iso_date>` form.

The panel can also be fetched on its own: `GET /panel/?item=<token>` renders
just the fragment (`views/_panel.py::panel_fragment`), which is what `base.html`
swaps in when a row is opened, so the list beside it is never rebuilt to change
which row is highlighted. The item is permission-checked there exactly as on a
full page load, and a token that resolves to nothing answers **204** — a stale
link is a panel with nothing in it, not an error — at which point the front end
closes the panel. Every row keeps a real `href`, so a modified click, a failed
fetch or no scripting at all falls through to ordinary navigation.

Every kind shares one frame: a pinned header, a pinned tab strip, a scrolling
body, and a pinned footer holding the actions. Actions either settle the item
in place or open a `<dialog>`; nothing navigates away.

> **Invariant.** A protocol save must post the *whole* form.
> `ProtocolAnswerForm.save()` deletes the `Answer` row for any field that
> arrives blank, so a partial post silently destroys answers.

### Read/unread

Alerts and chatbot conversations carry a per-user read mark (`SeenMark`), keyed
by the same panel token. Opening the panel is what marks an item read, and it
happens in exactly one place — `resolve_panel_item`. Marks are per user, so a
supervisor opening the queue does not clear a navigator's.

### Communications

`/communications/` is the cross-client queue: **Coming up** (what still needs
doing, soonest first) and **Happened** (what already did, newest first). It
replaces the old per-area list pages. Cancelled calls stay in the history,
struck through — "arranged and called off" is a different fact from "never
arranged" — and can be reinstated.

### Retired pages

All redirect; none 404. Ownership is checked *before* redirecting.

| Old route | Now |
| --- | --- |
| `/calls/<id>` (`pending_call`) | redirects to the panel on Communications |
| `/meetings/<id>/edit/` (`edit_meeting`) | GET redirects to the panel; **POST is live** — it backs the reschedule dialog |
| `/app/schedule_call` | redirects to Communications, where the scheduling dialog lives; POST still honoured |
| `/meetings/` (`meetings_list`) | removed entirely — Communications replaces it |
| `alerts.html` list page | removed entirely — Communications replaces it |

`patient_conversation_detail` is **not** retired: the client timeline and
`views/alerts.py` still link to it.

### Endpoints added with the redesign

All are `navigator_required` and ownership-checked.

| Route | Name | Purpose |
| --- | --- | --- |
| `POST /meetings/<id>/cancel/` | `cancel_meeting` | cancel a scheduled call, or reinstate with `reinstate=1` |
| `GET /panel/` | `panel_fragment` | the detail panel on its own, for `?item=` swaps; 204 when nothing resolves |
| `POST /notes/<kind>/<pk>/add/` | `add_note` | write a note on a meeting, recording, alert or conversation |
| `POST /notes/<pk>/edit/` | `edit_note` | change a note's body |
| `POST /notes/<pk>/delete/` | `delete_note` | remove a note; the panel offers Undo before it fires |
| `POST /patients/<pk>/chatbot/` | `toggle_patient_chatbot` | agent on/off per client, with reason + audit fields |
| `POST /patients/<pk>/raise-alert/` | `raise_alert` | human-raised alert (`data.raised_by_human=True`) |
| `POST /patients/<pk>/note/` | `save_client_note` | client-page notes |
| `POST /patients/<pk>/terms/` | `update_client_terms` | contact terms, agent and background |
| `POST /dashboard/next-call/hide/` | `hide_next_call` | dismiss the next-call card, or switch it off |
| `GET /alerts/since/` | `alerts_since` | poller behind the toast notifications |
| `GET/POST /communications/` | `communications` | the queue; POST is the scheduling dialog |

### Model changes (migrations 0057–0065)

| Model | Added |
| --- | --- |
| `Meeting` | `notes`, `notes_updated_at` (both **removed again in 0072** — see below); `modality` (PHONE/IN_PERSON) + `location`; `CANCELLED` status with `cancel_reason`, `cancelled_at` |
| `Caregiver` | `relationship`, `involvement` |
| `ContactTerm` | new model, 5 standard terms seeded; stored on `Patient.contact_terms` as a slug list |
| `SeenMark` | new model — per-user read marks, keyed by panel token (no FK: a chat item is a patient+day) |
| `Patient` | `chatbot_enabled`, `chatbot_off_reason`, `chatbot_off_at`, `chatbot_off_by` |
| `ConvAIUser` | `dashboard_prefs` (JSON) — next-call card state, per user rather than per browser |

The agent off-switch is **per client**, not per alert, and it genuinely gates
replies: `process_message_for_patient` in `utils.py` returns early. Both inbound
paths funnel through it. The caregiver's message is still recorded; what stops
is the answer.

### Model changes (migrations 0066–0072)

| Model | Change |
| --- | --- |
| `Note` | **new model** — one written note, with an author and created/updated times. The parent is an explicit nullable FK per kind (meeting / recording / alert / conversation) rather than a generic relation: more columns, but the queries stay simple and permission follows the parent's client. Replaces `Meeting.notes` and `alert.data['internal_note']`, which were single strings with no author, overwritten on every save |
| `CallRecording` | `transcript_segments`, `transcript_moments` — Whisper is called with `verbose_json` so the timings survive, and each key moment names a *segment index* rather than writing its own timestamp, so a moment the model invents has nothing to attach to and is dropped instead of pointing at silence |
| `Meeting` | `ended_at`, with a `happened_at` property falling back to `cancelled_at` then `scheduled_time`. Before this, completed calls sorted by their *scheduled* time, so "Happened" could contain future dates. `notes` / `notes_updated_at` **dropped** |
| `Answer` | `by_text` — the answer came back from the caregiver via the protocol automation rather than being typed by a navigator. Cleared when a human edits the answer |
| `SiteConfiguration` | `transcript_moments_prompt` — editable in Settings → Prompts, blank falls back to `default_prompts.DEFAULT_TRANSCRIPT_MOMENTS_PROMPT` |

Four of these move data and are **not cleanly reversible** — run them against a
copy of production first:

| Migration | What it moves |
| --- | --- |
| `0067_meeting_notes_to_note_rows` | the old `Meeting.notes` blob into `Note` rows |
| `0068_alert_internal_note_to_note_rows` | `alert.data['internal_note']` into `Note` rows |
| `0071_backfill_ended_at` | stamps `ended_at` on past-tense meetings still dated in the future |
| `0072_retire_legacy_note_fields` | carries `alert.data['note_log']` (a rolling list nothing ever displayed, and the one place holding notes `0068` did not cover) into `Note` rows, then drops the four dead alert keys and the two `Meeting` note columns |

### Environment sensitivities

- `SEND_CARE_PLAN=False` — the "Send to caregiver on WhatsApp" button is
  *hidden*, not disabled; the view 403s while the flag is off.
- **Start call** renders disabled, with a tooltip, unless both the navigator and
  the caregiver have phone numbers.
- With LLM/Twilio keys blank, Summarise / Transcribe / calls / chatbot replies
  fail with a message rather than a 500.

## 4. Project layout

```
Minder-Chat/
├── README.md, *.md              Topic guides (install, database, agents, …)
├── docs/
│   ├── technical-manual.md      This document
│   └── user-manual/             Static HTML user manual (publishable)
└── Django_CMS/                  The Django project
    ├── manage.py
    ├── Dockerfile               python:3.10-slim + gettext + Tailwind CLI
    ├── docker-compose.yml       web + db (Postgres 15), media volume
    ├── entrypoint.sh            Rebuilds Tailwind CSS on start
    ├── .env.sample              Annotated template for .env
    ├── requirements.txt         Pinned Python dependencies
    ├── tailwind/                Tailwind input.css + config
    ├── locale/                  Translations (en_GB, es_PE, pt_BR, it, ko, zh_Hans)
    ├── client_sdk/              Python API client package (downloadable in-app)
    ├── Django_CMS/              Project settings
    │   ├── settings.py          Entry point: loads .env, imports settings_app
    │   ├── settings_app.py      App settings (hosts, language, DB, security)
    │   └── settings_default.py  Base settings shared defaults
    └── ConvAI/                  The application
        ├── models.py            Patient, Caregiver, Meeting, Agent, Alert, …
        ├── views/               One module per area (patients, calls, chat, …)
        │   ├── _panel.py        Resolves ?item=<kind>-<id> into a panel item
        │   └── communications.py  The cross-client Coming up / Happened queue
        ├── api/                 DRF views, serializers, throttles, urls
        ├── native_agents/       In-process LangGraph agents (registry)
        ├── llm_factory.py       Provider-agnostic model factory
        ├── site_config.py       DB-override → env → settings resolution
        ├── async_reply.py       In-process worker pool for Twilio replies
        ├── roles.py             Role predicates & view decorators
        ├── security_headers.py  CSP & security headers middleware
        ├── templates/           Server-rendered UI (per-area folders)
        │   ├── _detail_panel.html   The right-hand panel, all item kinds
        │   └── communications/      The Coming up / Happened queue
        └── migrations/          Includes data migrations that seed roles/agents
```

## 5. Installation

Follow the [installation guide](../install_guide.md). In short:

```bash
cd Django_CMS/
cp .env.sample .env        # then edit: set DJANGO_SECRET_KEY, keys you need
docker compose build
docker compose up -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

The app is at <http://localhost:8000>. The `db` container is optional — any
PostgreSQL works (see [§7](#7-database)).

**Requirements:** Docker + Docker Compose. Nothing else on the host; the image
bundles Python 3.10, `gettext` (translations) and the Tailwind standalone CLI
(CSS build, no Node needed).

## 6. Configuration reference

All variables live in `Django_CMS/.env` (template: `.env.sample`). Settings
marked *runtime* can also be changed in **Settings** in the app, which takes
precedence over `.env`.

### Core

| Variable | Default | Description |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | dev-only fallback | **Required in production.** Django signing key. |
| `DJANGO_DEBUG` | `0` | Enable Django debug mode (never in production). |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,0.0.0.0` | Comma-separated deployment hosts. |
| `CSRF_TRUSTED_ORIGINS` | *(empty)* | Comma-separated origins incl. scheme. |
| `PLATFORM_LANG` | `English` | UI language: `English`, `Spanish`, `Portuguese`, `Italian`, `Korean`, `Chinese`. |
| `PLATFORM_TZ` | `Europe/London` | Display timezone. |
| `BRAND_NAME` / `BRAND_LOGO` | conversational-care.ai | Branding (*runtime*). |
| `ENABLE_API` | `1` | Expose the REST API under `/api/v1/`. |

### Database (see [database.md](../database.md))

| Variable | Description |
| --- | --- |
| `DB_ENGINE` `DB_NAME` `DB_USER` `DB_PASSWORD` `DB_HOST` `DB_PORT` | Standard connection settings; `DB_HOST=db` targets the bundled container. |
| `DB_SSLMODE` | `disable` locally, `require` for managed Postgres. |
| `POSTGRES_PUBLISH_PORT` | Host port publishing the bundled DB (compose only). |
| `WEB_PUBLISH_PORT` | Host port publishing the app (compose only, default `8000`). Set it when the host already has something on 8000 — e.g. a second copy of this project. |

### Twilio / messaging

| Variable | Description |
| --- | --- |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Twilio credentials (*runtime*). |
| `PLATFORM_PHONE` | The platform's WhatsApp/voice number (*runtime*). |
| `TWILIO_WEBHOOK_PATH` | Inbound webhook path (default `webhooks/whatsapp`). |
| `TWILIO_SMS_FROM` | SMS sender for the alerting system (*runtime*). |
| `SMS_TEMPLATE_START_INFECTION_SID` / `…_TEXT` | Infection-alert SMS template (*runtime*). |
| `WHATSAPP_TEMPLATE_SID` | Content template for meeting reminders. |
| `WHATSAPP_TEMPLATE_CARE_PLAN_SID` | Content template for care-plan sending (*runtime*). |
| `SEND_CARE_PLAN` | Allow sending care plans over WhatsApp (*runtime*). |
| `WHATSAPP_AUDIO_ENABLED` | Voice-note replies over WhatsApp (*runtime*). |
| `ASYNC_WHATSAPP_REPLY` / `WHATSAPP_WORKERS` | Async reply pool (see [§10](#10-messaging-twilio-webhook--async-replies)). |

### Agents & models (see [agents.md](../agents.md))

| Variable | Description |
| --- | --- |
| `DEFAULT_AGENT_MODEL` | Model for in-process agents without an explicit model, e.g. `openai/gpt-4.1-mini` (*runtime*). |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY` | Provider keys — only for providers you use (*runtime*). |
| `USE_AZURE` + `AZURE_*` endpoint/key vars | Route models through Azure instead of public APIs (*runtime*). |
| `AGENT_ALLOWED_HOSTS` | SSRF allow-list of hosts remote agents may point at (*runtime*). |
| `PROMPT_AGENT_TEMPERATURE` | Sampling temperature for prompt-based agents (default `0`). |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | Text-to-speech for voice replies (*runtime*). |

### Behaviour & storage

| Variable | Description |
| --- | --- |
| `HIDE_MEETING_STEPS` | Hide the step checklist on calls (*runtime*). |
| `ENABLE_AUTOMATIONS` | Enable automation features (*runtime*). |
| `SELF_REGISTRATION_ENABLED` / `SELF_REG_AGENT_NAME` | WhatsApp self-registration (see [§9](#9-agents)) (*runtime*). |
| `VOICE_RECORDINGS_DIR` / `CALL_RECORDINGS_DIR` | Override media dirs (default: under `MEDIA_ROOT`). |
| `DOWNLOAD_TOKEN_KEY` | Dedicated key signing single-use media tokens; random per-process if unset. |
| `SECURE_HSTS_SECONDS` | HSTS max-age (default 1 year). |

## 7. Database

Everything is stored in **PostgreSQL** — application data and the LangGraph
checkpoints used by in-process agents (same `DB_*` credentials, no separate
configuration). Three supported setups, plus backup/restore commands, are
documented in [database.md](../database.md):

1. **Bundled container** (default) — `docker compose up` starts Postgres 15
   alongside the app; data persists in the `postgres_data` volume.
2. **Port conflict on the host** — set `POSTGRES_PUBLISH_PORT`, everything
   else is untouched.
3. **External/managed Postgres** — set `DB_HOST`/`DB_SSLMODE=require` and
   start only `web` with `docker compose up -d --no-deps web`.

Backups use plain `pg_dump`/`pg_restore` through the `db` container.

## 8. Users, roles & permissions

The role model is small and explicit, built on Django groups — full detail in
[permissions.md](../permissions.md):

| Role | Identified by | Scope |
| --- | --- | --- |
| **Admin** | superuser (`access_configuration` permission) | Everything + Settings + user management |
| **Navigator (CTN)** | `Navigator` group | Own clients only; no configuration |
| **Test user** | `PatientTester` group, linked 1:1 to a client | The audio chatbot for that client only |
| **Client** | *not a user* — `Patient`/`Caregiver` rows | Interacts via WhatsApp/phone/chat |

Groups are seeded by a data migration. Developers enforce access with the
predicates and decorators in `ConvAI/roles.py` (`is_admin`,
`navigator_required`, …), re-exported via `ConvAI/views/_base.py`.

## 9. Agents

Full detail in [agents.md](../agents.md). Summary:

- Every chat surface resolves an **`Agent`** row and calls
  `generate_response_with_agent()`, which branches on `Agent.kind`:
  - **`remote`** — runs on your LangGraph server (`host:port`), invoked with
    `RemoteGraph`; restricted to `AGENT_ALLOWED_HOSTS` (SSRF guard).
  - **`prompt`** — in-process, single model call with a stored system prompt,
    no tools. Created freely by admins in the Agents UI.
  - **`native`** — ships with the platform, in-process compiled LangGraph
    graphs with Postgres-checkpointed state, registered via
    `@register(key)` in `ConvAI/native_agents/`.
- Bundled native agents: **Loopback** (echo/connectivity reference),
  **Link Worker** (the navigator chatbot bubble; scaffolded), the
  **Protocol Q&A** agent, and the **Self Registration** agent that answers the
  WhatsApp QR flow (Settings → Registrations).
- **Model selection** is per agent (`Agent.model`, e.g.
  `anthropic/claude-sonnet-4-6`), built by the shared factory
  `llm_factory.make_llm()`; provider inferred from the prefix, keys resolved
  at runtime (DB → env), optional Azure routing.

To add a native agent: create a module in `ConvAI/native_agents/` whose
builder returns a compiled graph, decorate with `@register("your_key")`, keep
optional imports inside the builder, and seed an `Agent` row with a data
migration.

## 10. Messaging: Twilio webhook & async replies

Inbound WhatsApp/SMS hits `TWILIO_WEBHOOK_PATH` (default
`/webhooks/whatsapp`). Generating a reply may involve LLM calls, speech-to-
text and TTS — longer than Twilio's webhook timeout. With
`ASYNC_WHATSAPP_REPLY=1`, the webhook returns an empty TwiML acknowledgement
immediately and hands the work to a **self-contained in-process thread pool**
(`WHATSAPP_WORKERS` threads; no Redis or broker). The worker then pushes the
reply out through the Twilio REST API. Design, sizing guidance and operational
notes: [async_replies.md](../async_replies.md).

## 11. File storage & media security

All user files (care plans, TTS audio, voice notes, call recordings, brand
logo) live on one persistent volume (`media_data:/media`), one folder per
category. **Only `branding/` is public.** Everything else is served through:

1. **Ownership-checked views** — the signed-in user must be allowed to see
   *that* file, or the view returns 403; and
2. **Signed, single-use, short-lived tokens** for Twilio's servers (HMAC-SHA256
   with `DOWNLOAD_TOKEN_KEY`, TTL minutes, consumed on first fetch).

Reverse proxies must **not** blanket-serve `/media/` — expose only
`/media/branding/`. Full detail and nginx snippet:
[file_storage.md](../file_storage.md).

## 12. Internationalisation

The UI ships in **six languages**: English (`en_GB`), Spanish (`es_PE`),
Portuguese (`pt_BR`), Italian (`it`), Korean (`ko`) and Simplified Chinese
(`zh_Hans`). Message IDs in code/templates are English; translations live in
`Django_CMS/locale/<code>/LC_MESSAGES/django.po`.

- **Deployment default:** `PLATFORM_LANG` in `.env` (`English`, `Spanish`,
  `Portuguese`, `Italian`, `Korean`, `Chinese`).
- **Per user:** each user can pick an interface language on their Profile page
  (stored on `ConvAIUser.preferred_language`; blank follows the system).

Translation workflow (run inside the `web` container, which has `gettext`):

```bash
# 1. Extract new/changed strings from code and templates into the .po files
docker compose exec web python manage.py makemessages -a --no-obsolete

# 2. Edit locale/<code>/LC_MESSAGES/django.po (fill msgstr, resolve fuzzy)

# 3. Compile and restart
docker compose exec web python manage.py compilemessages
docker compose restart web
```

**Adding a language:** run `makemessages -l <code>`, translate the new `.po`,
then register the code in `LANGUAGES` (both `settings_default.py` and
`settings_app.py`), map it from `PLATFORM_LANG` in `settings_app.py`, and add
it to `ConvAIUser.Language` choices (plus a migration). The `zh_Hans` commit
is a complete worked example.

Keep plural entries **format-check clean**: every placeholder used in a
translation must exist in `msgid_plural` (CI-friendly check:
`msgfmt --check`). Both `{% blocktrans %}` branches should bind the same
variables.

> **Watch the fuzzy entries.** `makemessages` marks an entry `#, fuzzy` when it
> guesses a new msgid from a similar old one, and **`msgfmt` skips fuzzy
> entries**, so a "translated-looking" `.po` can still render English. Worse,
> the guesses can be wrong in meaning — a past run paired *How* with *Low*,
> *Cancelled* with *Cancel*, and *In person* with *In progress*. Always review
> fuzzies rather than clearing the flag, and check both counts before shipping:
>
> ```bash
> msgattrib --untranslated locale/<code>/LC_MESSAGES/django.po | grep -c '^msgid'
> msgattrib --only-fuzzy    locale/<code>/LC_MESSAGES/django.po | grep -c '^msgid'
> ```
>
> All six catalogues are currently at zero untranslated and zero fuzzy. If you
> clear a fuzzy flag by hand, delete the stale `#|` previous-msgid comment with
> it, or `msgfmt` will reject the file.

## 13. REST API & Python client

The API is enabled by default (`ENABLE_API=1`) and mounted under `/api/v1/`.
Authentication is **DRF token auth** (`Authorization: Token <token>`); each
user generates a personal token on their **Profile** page (shown once;
regenerating revokes the old one). Permissions mirror the web app: navigators
act on their own clients, admins on everybody. Throttles: 120 req/min per
user, 60/min on `/messages/`.

| Endpoint | Methods | Purpose |
| --- | --- | --- |
| `/api/v1/messages/` | POST | Send a message to a patient's agent and get the reply |
| `/api/v1/patients/` | GET | List/search visible patients |
| `/api/v1/patients/<id>/meetings/` | GET | Meetings for a patient |
| `/api/v1/patients/<id>/protocols/filled/` | GET | Protocols already answered |
| `/api/v1/patients/<id>/details/append/` | POST | Append to the patient's details |
| `/api/v1/meetings/` | POST | Schedule a meeting |
| `/api/v1/meetings/<id>/answers/` | PUT/PATCH | Upsert protocol answers |
| `/api/v1/protocols/<meeting>/<num>/` | GET | Protocol content for a meeting |
| `/api/v1/protocols/<num>/questions/` | GET | Protocol question structure |
| `/api/v1/alerts/` · `/alerts/<id>/` · `/alerts/<id>/resolve/` | GET/POST/PATCH | Alert lifecycle |
| `/api/v1/self-registrations/` | POST | Create a pre-registration |

A ready-made **Python client** (`conversationalcare_api`) lives in
`Django_CMS/client_sdk/` and is downloadable from the app itself
(**Settings → API client**, which shows install commands pre-filled with your
platform URL). Usage: [client_sdk/README.md](../Django_CMS/client_sdk/README.md).

## 14. Security & deployment checklist

Hardening already built in: CSRF with HttpOnly cookie, HSTS + nosniff +
security-headers middleware (`ConvAI/security_headers.py`), SSRF allow-list
for remote agents, signed single-use media tokens, ownership-checked media
views, DRF throttling, write-only secret fields in Settings.

Before going to production:

- [ ] Set a strong **`DJANGO_SECRET_KEY`** (the committed fallback is dev-only).
- [ ] Set **`DOWNLOAD_TOKEN_KEY`** so signed media links survive restarts.
- [ ] `DJANGO_DEBUG=0` (default).
- [ ] Set `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` to your domain(s).
- [ ] Change the **`DB_PASSWORD`** default; use `DB_SSLMODE=require` off-box.
- [ ] Terminate TLS in front of the app (HSTS is on; cookies are secure-proxied
      via `SECURE_PROXY_SSL_HEADER`).
- [ ] Restrict `/media/` at the proxy to `branding/` only ([§11](#11-file-storage--media-security)).
- [ ] Keep `AGENT_ALLOWED_HOSTS` to the hosts your remote agents actually use.
- [ ] Configure the Twilio webhook URL (shown in **Settings → System**) and
      consider `ASYNC_WHATSAPP_REPLY=1`.
- [ ] Decide whether the REST API should be exposed (`ENABLE_API`).
- [ ] Set up database backups ([database.md](../database.md)).
- [ ] Confirm `collectstatic` picked up `ConvAI/static/app/fonts/`. The DM Sans
      and Material Icons faces are served from the app, not from Google — if
      those three `.woff2` files are missing, **every icon renders as its
      ligature text** (literally the words `home`, `settings`, `save`), which
      looks like a CSS failure but is a static-files one.

## 15. Development workflow

- **Run:** `docker compose up` — the source tree is bind-mounted, `runserver`
  reloads on change; the entrypoint rebuilds Tailwind CSS on start.
- **Migrations:** `docker compose exec web python manage.py makemigrations ConvAI`
  then `migrate`. Ship seeded data (roles, native agents) as data migrations.
- **Styling:** edit `tailwind/input.css` / template classes; the standalone
  Tailwind CLI compiles to `ConvAI/static/app/css/tailwind.css` (no Node).
- **Translations:** see [§12](#12-internationalisation) — please keep all six
  catalogs at 100% (`msgfmt --statistics --check`) when adding UI strings.
- **Checks:** `python manage.py check` must stay clean; a test suite is not
  yet in place (contributions welcome — `ConvAI/tests.py` is the stub).
- **Views/templates layout:** one module per area under `ConvAI/views/`,
  matching template folders under `ConvAI/templates/`.

---

*Conversational Care is developed at the Wellbeing Technologies Lab, Dyson
School of Design Engineering, Imperial College London, and licensed under
AGPL-3.0. See the [README](../README.md) for team, funding and citation.*
