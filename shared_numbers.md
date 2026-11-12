# Shared numbers

Two Conversational Care installations can answer the same WhatsApp/SMS
number. Twilio delivers every inbound message for a number to **one** webhook,
so one installation — the **hub** — receives everything, works out which
installation each message is for, and hands over the ones that are not its
own. Outbound needs nothing: each installation already sends through the
Twilio REST API with its own copy of the credentials.

Off until an installation is linked: with no enabled **linked installation**
the webhook does exactly what it did before.

## Why a hub, and not a router

The obvious alternative is a small service in front of both installations
that owns the webhook and dispatches. That is one more deployment to run,
secure and keep up for the sake of two installations — and it would still
have to ask each of them whose a number is, because that is where the
answer lives. The hub is the platform installation: it already receives the
webhook, already has a REST API with tokens, and already knows how to
validate and build a Twilio signature. Nothing new listens on the internet.

Nothing else is shared. The installations keep separate databases; the only
things that cross are a yes/no question about a number (never *whose* it is)
and the message itself, on its way to the installation that claimed it.

## The claim rule

Every installation can answer one question about a message: *is this
mine?* The rule is `claims(phone, body)` in `ConvAI/message_routing.py`, and
it is the same code whether the hub asks itself or a peer answers over the
API. A message is claimed when:

| | Reason | How it is decided |
|---|---|---|
| a | `client`, `caregiver` | The number is on a client's or caregiver's record (`message_attribution.resolve_inbound`). |
| a | `navigator` | It is a navigator's phone, linked for Link Worker on WhatsApp (`staff_whatsapp`). |
| b | `registration pending` | A `SelfRegistration` from this number is waiting for approval. |
| b | `registering` | This number wrote to the registration agent in the last 24 hours (a `Message` with `sender_role = prospect`). |
| c | `registration message` | The number is unknown, self-registration is on, and the text **starts with this installation's registration message** — case and whitespace aside. |

Anything else is `unknown number`, and not claimed.

Rule *c* is what lets one number serve two registration QR codes. Each
installation has its own **Registration message** (Settings → Registrations;
`SELF_REG_MESSAGE` in `.env`; default "Hi! I want to register", translated).
Its QR codes and shareable links pre-fill that message, so the first thing a
stranger sends says which installation they scanned. Once they have written
once they are `registering` for the next 24 hours, and once their request is
filed they are `registration pending`, so the rest of the exchange — which
could be anything — follows them without the message having to match again.

Give installations that share a number **different** registration messages.
With the same one, both would claim every stranger and priority would decide
— always the same installation.

## Priority

When more than one installation claims a message the lowest **priority**
number goes first.

| Where | Default |
|---|---|
| A linked installation (`LinkedInstallation.priority`) | 10 |
| The hub itself (Settings → Messaging → Linked installations; `SiteConfiguration.routing_priority`) | 100 |

So, left alone, a linked installation beats the hub, and on a tie a peer
still beats the hub: the hub's rank is meant to be the fallback. Two peers
with the same number are ordered by name, which is not a rule worth relying
on — give them different numbers.

## What happens to a message

`whatsapp_webhook` validates Twilio's signature as before, then calls
`message_routing.route(request)` — before the asynchronous and synchronous
branches, so a routed message is never also processed locally.

1. **Nothing to do** if no enabled linked installation exists, or the request
   carries `X-CC-Routed: 1` (it has already been routed once).
2. **Ask everybody.** The hub applies the claim rule locally and, in parallel
   threads, calls each enabled peer's
   `GET /api/v1/routing/claim/?phone=…&body=…` with that peer's API token,
   with a 2-second timeout. A timeout or an error is *no claim*, logged, and
   written to the peer's **Last error** on the Settings card; an answer sets
   its **Last contact**.
3. **Choose.** First claimant by priority. If that is the hub, or nobody
   claimed, the message continues locally exactly as today.
4. **Forward.** Otherwise the raw Twilio form is POSTed to the peer's webhook
   URL with a fresh `X-Twilio-Signature` computed for *that* URL with the
   shared `TWILIO_AUTH_TOKEN`, plus `X-CC-Routed: 1`; 10-second timeout, one
   retry on a connection failure. The peer's own webhook accepts it as if
   Twilio had called — same signature check, same code path, so its sync or
   async setting applies — and whatever it answers (status, body, content
   type) is relayed to Twilio.
5. **If both attempts fail**, the error is logged, written to the peer's row,
   and Twilio gets an empty `<Response></Response>`. The message is **not**
   processed on the hub: it was claimed by somebody else, and answering it
   from the wrong installation would be worse than a missed message. Because
   the forward is signed for the peer's URL, that URL must be exactly the one
   the peer builds for itself (what Twilio would be given), scheme included.

Every routed message leaves one info line in the hub's log naming the
destination and the reason, e.g. `Routed message from whatsapp:+44… to Reco
(registration message)`.

The budget matters: Twilio waits about fifteen seconds for the webhook. Two
seconds of asking plus the forward fits; a peer that regularly times out
should be looked at, not given longer.

## Settings → Messaging → Linked installations

Admin only. Lists every linked installation with its webhook URL, priority,
enabled state, last contact and last error, and offers:

- **Add** / **Edit** — name, webhook URL, API token (write-only: a blank
  field keeps the stored token), priority, enabled.
- **Test connection** — calls the peer's claim endpoint with a number nobody
  holds and records the outcome on the row, so a wrong URL or token shows up
  here rather than on the first real message.
- **Remove**.
- **This installation's priority** — the hub's own rank (see above).

The claim endpoint uses the ordinary REST token (Settings → API client) of a
user on the peer. It answers the verdict and what the hub needs to warn about
the peer, never whose the number is:

```json
{"claims": true, "reason": "client", "async_replies": true,
 "self_registration": true, "registration_message": "Hi! I want to join RECO"}
```

`registration_message` is blank while the peer's self-registration is off.
The hub keeps the last values it saw on the peer's row.

## What can go wrong

A misconfigured pair fails quietly: a message answered by the wrong
installation, or by none. Everything that can be detected is put where an
admin will see it. A tab holding an alarm gets a red dot in the Settings
sidebar, so it shows whichever tab is open.

**On the hub, Settings → Messaging → Linked installations**

| Alarm | Means | Fix |
|---|---|---|
| Red banner: *replies synchronously* | Linked installations are enabled and the hub runs with `ASYNC_WHATSAPP_REPLY` off. Each message first waits up to 2 s for the peers, then for the hub's whole reply, inside Twilio's 15 s. | `ASYNC_WHATSAPP_REPLY=1` on the hub, restart. |
| Red banner and red row: *wait for the same registration message* | Two installations on the number (hub included) wait for registration messages where one starts with the other. Every stranger goes to whichever ranks first. | A distinct message for each, under Settings → Registrations. |
| Red row: *Last call failed* | The last claim request or forward failed. The text says how. | See the Test connection messages below. |
| Red row: *Has never answered* | Linked but never reached. | Press **Test connection**. |
| Red row: *No answer since …* | No answer for 3 days. Every message asks every peer, so a number in use reaches each peer far more often. | Test connection; check the peer is up. |
| Amber row: *Replies synchronously* | The peer reported `async_replies: false`. A forwarded message waits for its whole reply. | `ASYNC_WHATSAPP_REPLY=1` on the peer. |
| *Failed forwards: N* | Messages the peer claimed but could not be given: both attempts failed, or it refused the signature (HTTP 403). Not reset automatically. | The row's last error says which. |

**Test connection** names each failure separately:

- *rejected the API token (HTTP 401)*: paste a valid token from the peer's Settings → API client.
- *has no claim endpoint (HTTP 404)*: the peer runs an older release, its REST API is off (`ENABLE_API`), or the URL points at the wrong host.
- *could not be reached (ConnectionError)*: DNS, TLS, firewall, or the peer is down.
- *did not answer within 2 seconds (timeout)*: up but too slow to route through.
- *not with the claim endpoint's JSON*: the URL reaches something else.
- *answered, but: …*: reachable, with a warning (synchronous replies, a colliding registration message).

**In the hub's log**

- A claim request that fails: warning, with the peer's name and the number masked (`+44…0001`).
- A message handled on the hub while a peer did not answer: warning, *handled here without an answer from Reco; it may have been theirs*.
- A forward that fails twice: error, and the message is not handled anywhere.
- A peer refusing the forward's signature: error naming the peer and the URL the forward was signed for.

Numbers in routing logs are always masked.

**On the peer**

- A request carrying `X-CC-Routed` whose signature does not validate is refused like any other bad request, and logged at warning level: the auth tokens differ, or the hub's webhook URL for this installation is not the URL it is served at.
- Settings → Registrations warns when the registration message is still the default and a hub has asked this installation about a message in the last 30 days. The claim endpoint writes down when it was last asked (`SiteConfiguration.routing_claimed_at`, at most hourly), which is how an installation that has no linked installations of its own knows it shares a number.

**Either side, Settings → Registrations**: on the hub, the same warning once a linked installation is enabled, plus any registration-message collision.

## Setting it up: reco beside platform

Twilio stays pointed at the platform installation; nothing changes there.

1. **Same Twilio credentials on reco.** `TWILIO_ACCOUNT_SID`,
   `TWILIO_AUTH_TOKEN` and `PLATFORM_PHONE` (Settings → Integrations, or
   `.env`) identical to platform's. The auth token is what makes a forwarded
   request verify; the phone is what reco sends from.
2. **A distinct registration message on reco.** Settings → Registrations →
   *Registration message*, e.g. "Hi! I want to join RECO". Reprint reco's QR
   codes from its Self registration tab afterwards — they carry the message.
3. **An API token on reco** for the hub to use: Settings → API client, any
   user.
4. **Link reco on platform.** Settings → Messaging → Linked installations →
   Add: reco's webhook URL (`https://reco.…/<TWILIO_WEBHOOK_PATH>`), that
   token, priority 10 (the default). Leave platform's own priority at 100, so
   anything both could answer goes to reco. Press **Test connection**.
5. **Run both with `ASYNC_WHATSAPP_REPLY=1`.** A forwarded message has
   already spent some of Twilio's fifteen seconds on the hub; reco answering
   from a worker and acknowledging at once keeps the whole exchange inside it
   (see [async_replies.md](async_replies.md)). The hub needs it for the
   messages it keeps, which now wait for the claim requests first. Both
   sides are flagged on the Settings card while it is off.

A client of reco's then writes to the shared number; platform asks reco, reco
says `client`, platform forwards, reco replies through the Twilio API from its
own credentials. A stranger scanning reco's QR sends "Hi! I want to join
RECO"; only reco claims it; the conversation with reco's registration agent
runs on reco from then on.

Linking is one-directional: reco never needs a linked installation of its
own, because nothing reaches reco that was not already routed.

## Code

| Where | What |
|---|---|
| `ConvAI/message_routing.py` | `claims`, `decide`, `forward`, `route`, `test_connection`, and the alarms: `peer_status`, `hub_warnings`, `registration_warnings`, `registration_collisions`. |
| `ConvAI/models.py` | `LinkedInstallation`; `SiteConfiguration.routing_priority`, `.self_reg_message`, `.routing_claimed_at` (migration `0104`). |
| `ConvAI/api/views.py` | `RoutingClaimView` — `GET /api/v1/routing/claim/`. |
| `ConvAI/registration.py` | `registration_message()`, used by the QR codes and the claim rule. |
| `ConvAI/views/settings_views.py` | The Linked installations card and its add / edit / delete / test views. |
| `ConvAI/test_message_routing.py` | Tests, including a forward accepted by the receiving webhook's real signature check. |
