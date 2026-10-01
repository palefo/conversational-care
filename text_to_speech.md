# Text to speech

Every spoken reply on the platform comes out of one function,
`ConvAI.tts.synthesize_speech`, and one setting decides who speaks it:

| Provider | `TTS_PROVIDER` | What it is |
|---|---|---|
| **ElevenLabs** | `elevenlabs` (and the default when unset) | The original. Multilingual voices from the ElevenLabs account. |
| **Azure AI Speech** | `azure` | Microsoft's neural voices, over one REST call to a Speech resource. Native voices for most languages, including 18 Brazilian Portuguese ones. |

**Settings → Agents → Text to speech** overrides `.env`, like every other runtime
setting. An installation that sets nothing keeps ElevenLabs and hears no change.

## Where it is used

| Surface | Voice |
|---|---|
| A client's WhatsApp voice note (inline and async) | the client's agent |
| The external chat's voice mode | the client's agent |
| The agent test page, voice mode | the agent under test |
| A link worker's voice note on WhatsApp | the Brazilian Portuguese voice if they spoke Portuguese, else Link Worker v2's — see [link_worker_whatsapp.md](link_worker_whatsapp.md) |

Real-time voice agents are not on this list: they speak through the Azure
OpenAI Realtime API and its own voice setting (`AZURE_REALTIME_VOICE`).

## Voices

**A voice belongs to a provider.** An ElevenLabs voice ID means nothing to Azure
and the other way round, so each agent keeps one of each — *ElevenLabs voice ID*
(`Agent.tts_voice_id`) and *Azure voice* (`Agent.azure_voice`) on the agent's
edit page — and the reply uses the one matching the provider that is on.
Switching provider never sends a voice to the wrong service.

Resolution, Azure: the agent's Azure voice → **Default Azure voice**
(`AZURE_SPEECH_VOICE`) → `en-GB-AdaMultilingualNeural`. That last one is
*multilingual*: it speaks whatever language the reply is in, so an agent nobody
has given a voice still sounds right to a Spanish or Portuguese speaker.

Resolution, ElevenLabs: the agent's voice ID → `ELEVENLABS_VOICE_ID` → the
built-in voice.

**Choosing an Azure voice.** The Azure voice fields are free text — any voice
name works — but they suggest every voice of the configured region as you type:
start with a language or a name (*Brazil*, *Sonia*, *multilingual*). The list
is fetched from the Speech resource once a day; without a key, or if Azure
cannot be reached, a short list of common voices is offered instead. Listen to
them in the voice gallery of Speech Studio (speech.microsoft.com), or Azure AI
Foundry.

## Setting up Azure AI Speech

1. Azure portal → **Create a resource** → **Speech** → region **UK South**
   (keeps audio in the UK) → tier **S0** (or F0, free, for trying it).
2. The resource → **Keys and Endpoint** → copy **KEY 1** and the **region**
   (`uksouth`).
3. **Settings → Agents → Text to speech**: *Voice replies are spoken by* →
   Azure AI Speech; paste the key and region (a pasted endpoint URL is read for
   its region). Or in `.env`: `TTS_PROVIDER=azure`, `AZURE_SPEECH_KEY`,
   `AZURE_SPEECH_REGION`.
4. Optionally a **Default Azure voice**, and an *Azure voice* on each agent.

## Decisions

**Text in, MP3 out — nothing else changes.** Azure is asked for
`audio-24khz-48kbitrate-mono-mp3`, the same container the ElevenLabs path
writes, so every caller's file naming, the signed download URL Twilio fetches,
storage and retention are untouched.

**A reply is model output, so it is escaped.** The text goes into SSML; it is
XML-escaped first and the voice name is a quoted attribute, so a reply cannot
close the `<voice>` element or inject markup.

**Failures are reported, not hidden.** A wrong key, an unknown voice, a timeout
— each raises `tts.TTSError` with the HTTP status and a hint (*check the key
and region*, *is this an Azure voice name?*). Callers keep the behaviour they
had: a link worker's voice note still gets its text; the agent test page shows
text only.

**No SDK.** One HTTPS POST with `requests`; nothing new in
`requirements.txt`.

## Configuration

| Setting | Default | What it does |
|---|---|---|
| `TTS_PROVIDER` | `elevenlabs` | `elevenlabs` or `azure`. |
| `AZURE_SPEECH_KEY` | — | The Speech resource's key. Write-only in Settings. |
| `AZURE_SPEECH_REGION` | — | e.g. `uksouth`. |
| `AZURE_SPEECH_VOICE` | `en-GB-AdaMultilingualNeural` | For agents with no Azure voice. |
| `LINK_WORKER_AZURE_VOICE_PT_BR` | `pt-BR-FranciscaNeural` | Link workers' Portuguese voice notes, under Azure. |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | — | ElevenLabs, under Settings → Integrations. |

All resolve Settings → `.env` → default.

## Where it lives

| Area | Location |
|---|---|
| Provider switch, voices, Azure call, voice list | `ConvAI/tts.py` |
| ElevenLabs call | `ConvAI/utils.py` (`synthesize_speech_elevenlabs`) |
| Settings | `SiteConfiguration.tts_provider`, `azure_speech_*`, `link_worker_azure_voice_pt_br`; `Agent.azure_voice`; migration `0094` |
| Voice suggestions | `{% azure_voice_datalist %}` (`templatetags/custom_filters.py`) |
| Tests | `ConvAI/test_tts.py` |
