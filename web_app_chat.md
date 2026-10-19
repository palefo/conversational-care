# Web app chat

A client's chat as an app on a phone or tablet, opened by scanning a QR code.
It was built for workshops: twenty people, a box of tablets with no SIM cards,
and nobody able to install WhatsApp on each one. The client talks to the same
agent, through the same pipeline, and their history is kept exactly as
WhatsApp's is. They can't reach any other part of Conversational Care.

**Off by default.** Turn it on in **Settings → Web app**, or with
`WEB_CHAT_ENABLED=1` in `.env` (the Settings value wins, as everywhere else).
While it is off:

- every `/c/` route returns 404;
- the client page has no Web app card;
- messages the platform starts go to the phone, even for a client whose card
  says "Web app".

Turning it off deletes nothing. Linked devices and channel choices come back
when it is turned on again, and the Settings tab says how many are waiting.

## Using it

1. Create the clients as usual. A phone number is not needed.
2. On the client page, open the **Web app chat** card and press
   **Show QR code**.
3. Scan the QR code with the tablet's camera. The page that opens names the
   client (first name only) and asks to **Open the chat**.
4. In Chrome on Android, **Install** (or ⋮ → **Add to Home screen**) puts the
   chat on the home screen. It then opens full screen, like an app. The chat
   page offers an Install button itself whenever Chrome allows it.
5. When the tablet is handed back, press **Unlink** next to it on the client
   page. Alternatively, the participant can use ⋮ → **Sign out of this
   device** in the chat.

**Messages the platform starts go to** (on the same card) chooses where a
protocol's questions and meeting reminders go: the phone, as before, or the web
app. A person chooses it; it is never inferred from a tablet being linked.
Replies always go back the way the client wrote, so a client who writes on
WhatsApp is answered on WhatsApp whatever this says.

**New QR code** issues a fresh code. The old one stops working, and every
device it let in is signed out. Use it when a code was lost or shown to the
wrong person.

The site must be on **HTTPS**. Without it, browsers will not install the app
or let it use the microphone, and the card says so.

## What the client sees

- The whole history of their conversations, as WhatsApp shows it, with day
  separators. That includes turns they had on WhatsApp or SMS: the chat
  belongs to the client, not the device.
- New messages, including ones the platform starts, which arrive within a few
  seconds while the chat is open. It polls `/c/messages/` every 4 s while
  visible, and immediately on coming back to the foreground. Nothing arrives
  while the app is closed, because there are no push notifications.
- Voice notes, **while WhatsApp audio is on** (Settings → General), so the
  two channels behave the same. A voice note is transcribed and answered by
  text, with a spoken reply attached when text to speech is configured.
- No Restart button. `/quit` still works if typed, as on WhatsApp.

## How it works

| Piece | Where |
|---|---|
| Switch, links, devices, history rows, delivery | `ConvAI/web_chat.py` |
| Device pages and staff actions | `ConvAI/views/webchat.py` |
| Chat page | `ConvAI/templates/webchat/chat.html` (look shared with the tester chat via `chat/_wa_styles.html`) |
| Models | `WebChatLink`, `WebChatDevice`; `Patient.chat_channel`; `Message.channel` (migration `0102`) |

### Getting in without a password

- **The QR carries a link, not a login:** `/c/join/<public_id>/<token>`. The
  token is an HMAC of the public id and the link's `nonce`, the same scheme as
  an online-meeting invite. Nothing secret is stored, so the QR can be shown
  again, and rotating the nonce (New QR code) kills every copy.
- **Joining is a POST**, from a page that names the client. Because of that, a
  link previewer or prefetch cannot create a device, and whoever holds the
  tablet sees whose chat it is before going in.
- **The device holds a random key** in an HttpOnly cookie scoped to `/c/`,
  valid for a year. Only its SHA-256 is stored, so a database dump lets nobody
  in. A device is live while it is not revoked and was joined through the
  link's current nonce.
- **The cookie is not a Django login.** It opens the `/c/` routes for one
  client and nothing else. A staff session open in the same browser gains
  nothing from it.
- Scanning a different client's code on a device signs it out of the first
  client's chat.

With study enrolment's `/join/`, these are the only unauthenticated pages in
the platform. Like those, they do not exist until the switch is on.

### Messages are the same messages

A typed turn goes through `process_message_for_patient`, the path WhatsApp and
SMS use. That means it gets the same agent and the same agent switch,
automations, safety review and alerts, Sensei passcode redaction, and thread
rollover. Each turn is stored as:

| Field | Value |
|---|---|
| `patient` | the client |
| `sender_role` | `client` |
| `channel` | `web` |
| `account` | none |
| `user` | `web:<device id>` |

A voice note takes the same path with its audio attached. (The WhatsApp voice
path still calls the agent directly; this one does not.)

Platform-started messages for a client on the web app are written straight into
their current conversation (`web_chat.deliver`) with `sender_role=platform`,
`channel=web`. Writing the message is the delivery, so there is no failure
branch and no SMS offer. This covers:

- **Protocol questions** ("Ask by text"): the first question opens the
  automation's thread there, and the answers come back as ordinary turns.
- **Meeting reminders**: a plain-text reminder addressed to the client. Online
  meetings still get their link from the meeting itself.

Not covered yet: the care plan (a WhatsApp media template) and online-meeting
invites. Both still go to a phone.

### `Message.channel`

This field is new, and is stamped from now on by:

| Value | Written by |
|---|---|
| `whatsapp` / `sms` | inbound WhatsApp and SMS, including WhatsApp voice notes |
| `web` | the web app chat |
| `tester` | the tester chat |

Older rows, and write sites with no channel to name, stay blank ("Not
recorded").

## Limits worth knowing

- **Anyone holding a linked device can read that client's chat**, which is
  what the join page warns about. Unlink tablets when they are handed back.
- **No notifications.** A protocol question waits in the chat until the app is
  opened.
- **No offline queue.** Without a connection, the message is put back in the
  box with a note to try again.
- Replies are generated while the request waits, as in the tester chat. A
  Sensei turn can take tens of seconds. If the request is cut off, the reply
  still arrives through the poll.

Tests: `ConvAI/test_web_chat.py`.
