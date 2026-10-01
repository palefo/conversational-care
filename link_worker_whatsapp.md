# Link Worker on WhatsApp (beta)

A link worker can ask Link Worker v2 about a client from their own phone, over
the service's WhatsApp number — on the way to a home visit, or at the door —
by text or by voice note.

**Off by default, and only with v2 on.** Settings → Agents → *Link workers can
use it on WhatsApp*. While off, WhatsApp behaves exactly as it did: no number is
treated as staff, no `LINK` message is answered, and the profile page has no
WhatsApp card.

## How a link worker uses it

1. **Link the phone.** Profile → *Link Worker on WhatsApp* → enter the mobile
   number → *Get a code*. Send `LINK 123456` (or `VINCULAR 123456`) from that
   phone to the service's WhatsApp number — the card offers a one-tap WhatsApp
   link and a QR code. The code works for 15 minutes and five tries.
2. **Load a client.** "What do I have today?" lists today's meetings (earlier
   ones included) and the week's. "Load Maria" — or any other of their own
   clients, to prepare for a later meeting or for any other reason.
3. **Ask.** "What did she say about her sleep?", "How has it changed?", "Who is
   her caregiver?", "Book a follow-up for Thursday at 10." Every reply starts
   with the loaded client's name, so it is never in doubt who it is about.
4. **Finish.** `/sair` (or `/end`) ends the session and unloads the client; a
   loaded client is also put down after four idle hours.

Voice notes are answered with a voice note — in the **Brazilian Portuguese
voice** when they spoke Portuguese — plus the text.

## Decisions

**One client at a time, chosen by the link worker.** The questions about a
client take no client id: they answer about the client on the link worker's
`StaffWhatsAppLink.loaded_patient`, which only `load_client` sets. The model
cannot drift onto somebody else; it can only load a client, which it is told to
do only when the link worker has said which — and every load is logged with the
reason given. Today's meetings are suggested because they are the obvious
choice, not the only one.

**The number is proven, not typed in.** A code issued on the profile page and
sent back from the phone. Stored only as an HMAC; five wrong tries spend it.

**A number that is also a client's or a caregiver's is never staff.** Refused at
linking, and refused again at every message if it turns up on a client record
later — the two cannot be told apart, so neither is guessed.

**Navigators only.** An admin sees every client: too much behind a phone
number. Admins use the web app.

**Narrower than the web.** No caseload-wide search on WhatsApp; the rest of v2's
questions, plus suggestions and loading. Same service (`client_records`), same
permission and privacy rules, same access log — marked *WhatsApp*.

**The link worker's own conversation.** Saved as their staff conversation, on no
client's file, never classified and never raising alerts.

**Voice by language.** Whisper reports the language spoken; Portuguese gets the
pt-BR voice from Settings, anything else the v2 agent's own voice. If the voice
cannot be produced the text still goes back.

## Configuration

| Setting | Default | What it does |
|---|---|---|
| `LINK_WORKER_WHATSAPP_ENABLED` | off | The switch. Needs `LINK_WORKER_V2_ENABLED` too. |
| `LINK_WORKER_VOICE_PT_BR` | — | ElevenLabs voice ID for replies to Portuguese voice notes. Blank uses the v2 agent's voice. |

Voice notes follow the platform's existing `WHATSAPP_AUDIO_ENABLED` switch, as
clients' do. The service's number is `PLATFORM_PHONE`.

**Choosing the Brazilian voice.** ElevenLabs → Voice Library → filter Language:
Portuguese, Accent: Brazilian → *Add to my voices* → copy the Voice ID into
Settings → Agents. The platform's speech model (`eleven_turbo_v2_5`) is
multilingual.

## Where it lives

| Area | Location |
|---|---|
| Linking, routing, sessions, voice | `ConvAI/staff_whatsapp.py` |
| The WhatsApp assistant | `ConvAI/native_agents/link_worker_v2.py` (`link_worker_whatsapp`), `prompts/link_worker_whatsapp.prompt` |
| Suggestions and loading | `ConvAI/client_records.py` (`visit_suggestions`, `load_client`) |
| Hooks | `utils.process_received_message` (text), `views.chat.whatsapp_webhook` and `tasks.job_process_whatsapp_audio` (voice notes) |
| Model | `StaffWhatsAppLink`, migration `0093` |
| Tests | `ConvAI/test_staff_whatsapp.py` |

## Limitations

* **Information leaves the platform.** Answers pass through Twilio and Meta and
  stay on the phone, including notification previews and backups. One client,
  chosen per visit and logged, is a much smaller exposure than a caseload — but
  it needs the lab's data-protection sign-off before real use.
* **A lost phone** answers about its loaded client until it goes idle (four
  hours) or the link is removed — from the profile page, or by an admin in
  Django admin → *Staff WhatsApp links*.
* **WhatsApp's own rules apply.** Replies are free-form only within 24 hours of
  the link worker's last message; the assistant never writes first.
* **Voice depends on ElevenLabs.** While the account is unpaid, replies arrive as
  text only.
* **It reads, and books meetings.** Dictating a note back after a visit would be
  the first time it writes a record; left for a separate decision.
