# Message export

Admins can download every stored message as a single CSV file, for analysis
outside the platform (behavioural analyses, study data sets). The export is
**off by default**. Most installations have no reason to let conversations
leave the platform in bulk, so each one has to switch it on deliberately.

## Turning it on

Either:

- **Settings → Export → Enable message export → On**, which takes effect
  immediately, or
- `MESSAGE_EXPORT_ENABLED=1` in `Django_CMS/.env`, then restart.

The setting in the app wins over `.env`, the same as every other runtime
setting. Leaving it at *Use .env default* follows `.env`. Setting it to *Off*
disables export even when `.env` says `1`.

While export is off, the Export tab shows only the switch, and the download URL
(`/app/export/messages.csv`) returns 404.

## Who can download

Admins only: users with the `access_configuration` permission (see
[permissions.md](permissions.md)). Navigators cannot, even for their own
clients. Every download is logged (`ConvAI.views.exports`) with the user's id
and the date range.

## Downloading

Settings → Export → pick an optional **From** and **To** date → **Download
CSV**. Both days are included, and days follow the platform timezone
(`PLATFORM_TZ`). Leave both empty to export everything.

The same file is available directly at:

```
/app/export/messages.csv?from=2026-09-01&to=2026-09-30
```

## The file

UTF-8 with a byte-order mark, so Excel shows accented, Korean and Chinese text
correctly. pandas and R's `readr` skip the mark.

**One row per message.** The platform stores a client's message and the agent's
reply to it as one record. The export splits that record into two rows, one per
speaker, and both rows keep the same `message_id` so they can be paired again.
Messages the platform sent on its own (meeting reminders, alert messages, care
plans) have only the platform's row. An empty side of an exchange is not
exported.

Rows are grouped by conversation and in order within each one.

| Column | Meaning |
| --- | --- |
| `message_id` | Stored exchange the message belongs to. A client's message and the reply to it share one. |
| `conversation_id` | Conversation UUID. Platform-sent messages have a key like `reminder-12`, `alert-7` or `careplan-3` instead. |
| `conversation_kind` | `chat`, `reminder`, `alert`, `care_plan`, or `other`. |
| `turn` | Position of the exchange within its conversation, from 1. It counts from the conversation's real start, even when the date range cuts into it. |
| `timestamp_utc` | When the exchange was stored, in UTC, ISO 8601. Both halves of an exchange carry the same time: the record is written once the reply exists. |
| `timestamp_local` | The same moment in the platform timezone, with its offset. Useful for time-of-day analyses. |
| `speaker` | `client`, `caregiver`, `app_user` (someone signed in: a test user or an API client), `unknown`, `agent`, or `platform`. |
| `patient_id` | Internal id of the client the message concerns. Empty when it cannot be worked out. |
| `app_user_id` | Internal id of the platform account a chat came from. Empty for WhatsApp/SMS traffic. |
| `agent_id` / `agent_name` | Agent attached to the conversation. |
| `text` | Message text. |
| `has_audio` | `1` if the message was a voice note, or the reply was sent as audio. |
| `liked` / `disliked` / `warning` / `dangerous` | Staff feedback on the agent's reply (`1`/`0`). Empty on client rows. |

### How speakers and clients are worked out

The stored record keeps the sender's phone number or email address. The export
uses it to decide who spoke, then leaves it out of the file:

- It's `client` if it is a client's own number or address, and `caregiver` if
  it is a caregiver's. A self-registered client is their own caregiver, with the
  same number on both records, and counts as `client`.
- It's `app_user` if the chat came from a signed-in platform account.
- Otherwise it's `unknown`.

`patient_id` comes from the conversation when it is linked to a client, and from
the meeting, alert or client that a platform-sent message points at. Failing
both, it comes from the sender's number, but only when that number belongs to
exactly one client. A caregiver who looks after two clients cannot say which one
a message was about.

## Privacy

- No names, phone numbers or email addresses are written. People appear only as
  internal ids.
- **Message text is exported as written**, and it can contain anything a client
  chose to type, including names, addresses or health details. The export does
  not try to scrub it. Treat the file as the sensitive data it is.
- Sensei `/login` passcodes never reach the file, because they are removed
  before a message is stored (see [agents.md](agents.md)).
- Text starting with `=`, `+`, `-` or `@` gets a leading `'`, so a spreadsheet
  does not run a message as a formula ("CSV injection"). Strip a leading `'` in
  analysis code if exact text matters.
