# File storage & media security

Conversational Care handles several kinds of user files. This page explains
**where they live** (so the setup is container-friendly) and **how access is
controlled** (so a leaked link does not expose private data).

## What files exist

| Category | Produced by | Contains |
| --- | --- | --- |
| **Care plans** | Navigator uploads a PDF | Clinical care-plan documents |
| **TTS audio** | ElevenLabs text-to-speech | Spoken agent replies (sent to users, incl. via Twilio) |
| **User voice notes** | Inbound WhatsApp / web audio | Audio messages recorded by caregivers/clients |
| **Call recordings** | Downloaded from Twilio | Recordings of phone calls |
| **RAG documents** | Admin uploads on an agent's Knowledge base page | Source documents a RAG-based agent searches |
| Brand logo | Admin uploads in Settings | The logo shown on the login page (**public**) |

Everything except the brand logo is **private**.

## Where files live (container-friendly)

All files are stored on a single persistent volume — `MEDIA_ROOT` — organised
into one directory per category:

```
media/                    ← MEDIA_ROOT, a persistent Docker volume (media_data:/media)
├── branding/             public   — brand logo (login page)
├── care_plans/           private  — uploaded care-plan PDFs
├── voice/                private  — ElevenLabs TTS + inbound user voice notes
├── call_recordings/      private  — call recordings downloaded from Twilio
└── rag_documents/<agent>/ private — documents uploaded to a RAG agent
```

Key points for running in containers:

- **One persistent volume.** `docker-compose.yml` mounts `media_data:/media`,
  and in the container `MEDIA_ROOT` resolves to `/media`. Every category lives
  under it, so nothing is lost on rebuild/restart.
- **Nothing is written into the code tree.** Previously voice files went to
  `./recordings` (inside the `.:/app` bind-mount). They now default to
  `media/voice` on the persistent volume.
- **Paths derive from `MEDIA_ROOT`.** `VOICE_RECORDINGS_DIR` and
  `CALL_RECORDINGS_DIR` (in `settings_default.py`) default to
  `MEDIA_ROOT/voice` and `MEDIA_ROOT/call_recordings`. Override them via env
  only if you mount storage elsewhere:

  ```env
  # Optional — defaults derive from MEDIA_ROOT
  VOICE_RECORDINGS_DIR=/media/voice
  CALL_RECORDINGS_DIR=/media/call_recordings
  ```

> **Upgrading an existing deployment:** if you previously ran with
> `VOICE_RECORDINGS_DIR=./recordings`, move that folder's contents into
> `media/voice` (or keep the old env override) so historic audio stays
> reachable. New files always go to the configured directory.

## How access is controlled

There is **no public URL for private files.** `/media/` serves **only**
`branding/`. Everything else is reachable through exactly two paths:

### 1. Ownership-checked views (for people using the app)

Each download view verifies the signed-in user is allowed to see that specific
file — a link alone is never enough.

| File | View | Who may access |
| --- | --- | --- |
| Care plan (view/download) | `view_care_plan`, `download_care_plan` | Admins and the client's assigned navigator |
| Call recording | `serve_protected_file` | Admins, and the navigator of the client whose number matches the recording |
| Message audio | `serve_audio_file` | Admins, and the test user linked to that client |

RAG documents have **no download view at all**. Nothing serves the stored file:
it is written once by the upload, read once by the ingestion worker, and kept
only so a failed ingestion can be retried without re-uploading. What people
read is the extracted text, through the agent. Files are removed with the
document (or with the agent) rather than being left behind.

If the check fails the view returns `403 Forbidden` — even for a valid,
existing file.

### 2. Signed, single-use, short-lived tokens (for Twilio)

Twilio's servers must fetch media directly (e.g. a voice reply or a care-plan
PDF attached to a WhatsApp message), so they cannot present a login session.
Instead they receive a URL carrying a token that is:

- **Signed** with a dedicated `DOWNLOAD_TOKEN_KEY` (HMAC-SHA256), so it cannot
  be forged;
- **Time-limited** (a short TTL — e.g. 10 minutes for audio, 15 for care plans);
- **Single-use** — consumed on first fetch (tracked in the cache), so a
  captured link cannot be replayed.

These are issued by `build_signed_download_token()` and validated by
`verify_and_consume_download_token()` (`ConvAI/utils.py`), and served by
`twilio_audio_download` and `twilio_careplan_download`. When you add a new file
type that Twilio must fetch, reuse this same mechanism — never hand Twilio a
raw `/media/` URL.

## Production / reverse-proxy guidance

Because private files sit under the same `MEDIA_ROOT` volume, the web server in
front of the app **must not** blanket-serve `/media/`. Expose only the public
branding subtree, and let the app serve everything else:

```nginx
# Public: brand logo only
location /media/branding/ {
    alias /media/branding/;
}
# Everything else under /media/ is private — do NOT add a location for it.
# Care plans, voice, and recordings are served by Django views with auth.
```

In development Django enforces the same rule: `DEBUG` static serving is scoped
to `/media/branding/` only (see `Django_CMS/urls.py`).

## Future work

- **Twilio download hardening** — the inbound download paths
  (`download_recording_mp3`, `download_twilio_media`) will be revisited
  separately.
- **Object storage** — the layout maps cleanly onto an S3/GCS bucket later:
  keep `branding/` public and serve the private prefixes via signed URLs. No
  application logic outside the storage layer needs to change.
