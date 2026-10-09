# Online meetings

Meetings in the browser between a navigator and a client or caregiver: a link
instead of a phone number, audio recorded per speaker and transcribed with
names, and an optional **AI voice interviewer** that works through a protocol
out loud while the navigator listens — or an assistant the navigator asks by
push-to-talk. No Twilio involved.

**Off by default, and optional all the way down.** The feature is switched on
in **Settings → Online meetings** (or `ONLINE_MEETINGS_ENABLED`). The media
server and agents only run with `--profile meetings`. The whole app can be left
out with `MEETINGS_APP=0`. See [Switching it off](#switching-it-off).

## What a navigator sees

1. Book a meeting as usual and choose **How → Online meeting** (offered only
   while the feature is on). Or, from a client's page, *Call → Start an online
   meeting instead*.
2. The meeting's panel shows **Open the meeting room**, **Copy link** and
   **Send link** (email with a calendar invitation, SMS, or WhatsApp inside the
   24-hour window). Email reminders for online meetings carry the link.
3. The room opens in **its own window**, so the app stays usable alongside it
   (and the app's call bar shows "In an online meeting · 12:04 · Back to
   meeting"). A pre-join screen checks the microphone first.
4. When the client opens their link they **knock**; the navigator admits or
   turns them away (or ticks *Admit automatically*).
5. **Interview** tab: pick a protocol and who to ask, and the AI interviewer
   joins, asks the questions one at a time, and the answers appear in a live
   checklist as they are saved. Pause, resume, *Tell it…* (a typed instruction)
   or stop at any time.
6. **Ask the assistant** (bottom bar, hold to talk): a prompt-based agent
   answers out loud; everyone in the meeting hears the answer.
7. **End** closes the room for everyone and asks how it went (the same outcomes
   as a call). The recording and its transcript appear in the panel when the
   background worker has mixed and transcribed them.

## What a client sees

A link (`/m/<id>/<token>`) opens a page with a **Continue** button, then a
lobby on the same page as the meeting: camera preview (off by default), a
microphone meter, a speaker test, the recording notice, **Join the meeting**,
and a waiting state until they are let in. Nothing to install; built for phones
first. Client pages use the public teal palette; staff pages the staff blue
(see [STYLING.md](STYLING.md)).

## Architecture

```
 browser (staff) ─┐                       ┌─ agent-scribe   (records)
 browser (client) ┼── WebRTC ── LiveKit ──┼─ agent-voice    (interviewer / assistant) ── Azure OpenAI Realtime
                  │   (UDP 7882/TCP 7881) │
                  └─ HTTPS ── Django ─────┴── internal API (service key + run token)
                               │  webhooks ◀── LiveKit
                               └─ Job queue ──▶ worker (mixdown, transcription)
```

- **LiveKit** (self-hosted, single node, no Redis, no Egress) carries the media.
  Django never touches it: it only mints access tokens (PyJWT) and calls
  LiveKit's server API over plain HTTP — no LiveKit SDK in the web image.
- **The agents** run from their own image (`Django_CMS/meeting_agents`, Python
  3.12, `livekit-agents==1.8.5`), separate because livekit-agents needs a far
  newer `openai` than the web app pins. They never import Django; everything
  comes over the internal API.
- **Recording** is done by a recorder agent that writes one wall-clock-aligned
  Ogg/Opus file per speaker (16 kHz mono, ~11 MB/hour). After the meeting the
  `meetings.finalize_recording` job mixes them into one MP3 for listening and a
  `CallRecording` (owned by the core app) with the per-speaker tracks, speaker
  names and roles — so the transcript says "Ana (caregiver)" rather than
  "Speaker 2". Video is never recorded.

### Why no Egress

LiveKit's own recorder (Egress) needs Redis and its own container, and its
composite mode runs headless Chrome (2–4 GB). The in-room recorder needs
neither, and writes exactly the per-speaker files the transcript wants. The
mixdown reads only `RecordingSegment` rows, so moving to Egress later (Track
Egress, at scale) changes nothing downstream.

## Identity and safety

- **No client accounts.** A link is the credential: `HMAC-SHA256(MEETING_LINK_KEY,
  "<public_id>:<nonce>")`, 128 bits. Nothing secret is stored, so a link can be
  re-sent; rotating the nonce (*New link*) kills every copy. Invites carry a
  `principal` field — the seam for client accounts later.
- **A GET on a link changes nothing**: mail scanners open every link. The
  *Continue* POST binds the browser and redirects to `/m/lobby`, so the token
  leaves the address bar. Link pages send `Referrer-Policy: same-origin` and
  `Cache-Control: no-store`; every failure shows the same generic page.
- **Window**: a link works from 30 minutes before to 3 hours after the meeting's
  *current* time (configurable) — a rescheduled meeting keeps its link — and
  always while a room is open. It stops on cancel, on moving the meeting to
  another client, or on changing it away from online.
- **No LiveKit token until admitted.** The lobby only polls Django.
- **Tokens**: one room, one identity, 15 minutes, `canUpdateOwnMetadata=false`
  (the role and recording flags the recorder trusts cannot be rewritten by a
  browser), clients cannot publish data. Room names carry no personal data.
- **Agents' internal API** needs three things: the `MEETINGS_SERVICE_KEY`, a
  signed run token naming one agent run (from the dispatch metadata, which only
  the job sees), and the run still being live in the database. An interviewer
  can only read and answer *its* protocol for *its* meeting. The recorder may
  hand in its last segments for ten minutes after the room closes.
- **Staff steer agents** with room RPCs; agents accept them only from
  `staff-<id>` identities, which only the platform can issue.
- **Answers are conflict-safe.** Panels post only the fields that were edited,
  with the value they were edited from; an answer that changed meanwhile (the
  interviewer, a WhatsApp reply) is never overwritten or erased — the person is
  asked which to keep. Voice answers are tinted violet (`Answer.source`).
- A WhatsApp protocol automation and a voice interview cannot run on the same
  meeting at once.

## Configuration

Behaviour — **Settings → Online meetings** (`meetings.MeetingsSettings`):

| Setting | Default | |
|---|---|---|
| Online meetings | off (`ONLINE_MEETINGS_ENABLED`) | The switch. |
| Record audio | on | Per-speaker audio, transcribed in the background. |
| Let invited clients straight in | off | Admit without asking once a navigator has joined. |
| Link opens / closes | 30 min before / 3 h after | |
| Meetings at the same time | 4 | Protects a small server. |
| Longest meeting | 90 min | Rooms open longer are closed by the reconcile job. |
| Assistant agent | none | A prompt-based agent: its prompt, and its knowledge base if it has one. |
| Agent voice | Azure Realtime voice | |

The interviewer and assistant use the **Azure OpenAI Realtime** settings in
Settings → Agents (endpoint, key, deployment, voice), handed to the agent per
run. Without them the agent reports "Azure OpenAI Realtime is not configured"
in the room instead of joining.

Connection — **`.env` only** (the web app, LiveKit and the agents must agree):

| Variable | Example | |
|---|---|---|
| `LIVEKIT_URL` | `ws://localhost:7880` / `wss://lk.example.org` | What browsers connect to. |
| `LIVEKIT_API_URL` | `http://livekit:7880` | How Django reaches LiveKit's API in compose. |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | long random strings | One key pair, shared by all three. |
| `MEETINGS_SERVICE_KEY` | long random string | Agents ↔ Django. Without it, no recording or agents. |
| `LIVEKIT_NODE_IP` | `127.0.0.1` / LAN IP / public IP | The IP browsers reach the media port on. |
| `MEETINGS_PUBLIC_BASE_URL` | `https://care.example.org` | Base for links (falls back to `AGENT_CALLBACK_URL`). |
| `MEETING_LINK_KEY` | optional | Signs links; derived from `DJANGO_SECRET_KEY` if blank. |
| `CC_DENOISE` | blank | Reserved: the denoising seam (below). |

## Running it

```bash
# Local (both browsers on this machine; Chrome treats localhost as secure):
docker compose -f docker-compose.yml -f docker-compose.meetings.yml --profile meetings up -d

# A server with a domain: also start the TLS edge (Caddy, automatic HTTPS) —
# browsers only give a page the microphone over HTTPS.
APP_DOMAIN=care.example.org LIVEKIT_DOMAIN=lk.example.org \
docker compose -f docker-compose.yml -f docker-compose.meetings.yml --profile meetings --profile edge up -d
```

Open **UDP 7882** and **TCP 7881** to the server (media), and 80/443 for the
edge. On networks that only allow 443 (some hospitals), TURN over TLS on 443 is
needed — planned, not built (`LIVEKIT_TURN_ENABLED` turns on UDP TURN only).

### Resources

| Service | Idle | Per active meeting |
|---|---|---|
| `livekit` | ~60 MB | +5–10 MB and a little CPU per participant |
| `agent-scribe` | ~120 MB | +~10 MB (threads, no inference) |
| `agent-voice` | ~110 MB | +250–350 MB per agent (one process each, none kept warm) |
| `worker` | ~150 MB | ffmpeg briefly during a mixdown |

## Switching it off

| | Web process | Containers | What changes |
|---|---|---|---|
| Feature off (Settings) | +~1.5 MB (the app's modules; no LiveKit code) | none needed | Online is not offered when booking; links show the generic "not available" page; existing online meetings stay readable (outcome, notes, recordings). A room already open is allowed to finish. The Settings tab stays visible. |
| `MEETINGS_APP=0` | nothing | none | The app is not installed: no URLs, no tab. The core reaches it only through `ConvAI/extensions.py`, whose defaults answer "not available". Recordings already made are core `CallRecording` rows and still play and re-transcribe. Run `python manage.py migrate meetings zero` first if removing the app for good. |
| Profile not started | — | none | The panel says the meeting server is not set up. |

## Denoising

Not done beyond what browsers already apply (echo cancellation, noise
suppression, gain control are requested on every microphone). The seam is
`meeting_agents/cc_agents/processors.py`: return an
`rtc.FrameProcessor[rtc.AudioFrame]` there and it is applied to everything the
agents hear and everything the recorder writes. In the browser, a processor can
be attached to the published microphone track (`room-core.js`, `onLocalMic`).

## Code map

| Path | |
|---|---|
| `Django_CMS/meetings/` | The app: models, links, LiveKit API, rooms, invites, views (staff, public, internal, webhooks), recording/mixdown, job handlers, Settings tab, templates and static. |
| `Django_CMS/meetings/static/meetings/` | `room-core.js` (shared room engine), `staff-room.js`, `lobby.js`, `room.css`, vendored `livekit-client` 2.22.3 (Apache-2.0). |
| `Django_CMS/meetings/prompts/protocol_interviewer_voice.prompt` | The interviewer's spoken-style prompt. |
| `Django_CMS/meeting_agents/` | The agents' image: `cc_agents/scribe.py`, `cc_agents/voice.py`, `cc_agents/wallclock.py`, `cc_agents/api.py`. |
| `Django_CMS/ConvAI/extensions.py` | The seam between the core and optional apps (provider + signals). |
| `Django_CMS/docker-compose.meetings.yml`, `Django_CMS/deploy/Caddyfile` | The media server, agents and TLS edge. |

Tests: `python manage.py test ConvAI meetings --settings=test_settings`
(the app's tests are under `meetings/tests/`), and for the agent image
`pytest meeting_agents/tests`.
