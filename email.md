# Outbound email

Conversational Care sends email for three things:

- **Password recovery** — the "Forgot your password?" link on the sign-in page.
- **Meeting reminders**, when the reminder channel is set to email instead of
  WhatsApp.
- **A test message**, sent by hand from Settings → Email to prove the
  configuration works.

All three go through one provider, chosen at runtime. Two are supported:
**Azure Communication Services** and **SMTP**.

## How it works

Everything that sends mail — including Django's own password-reset machinery,
deep inside `django.contrib.auth` — calls `django.core.mail`. That routes to
`EMAIL_BACKEND`, which this project points at
[`ConvAI/mailer.py`](Django_CMS/ConvAI/mailer.py):

```python
EMAIL_BACKEND = "ConvAI.mailer.PlatformEmailBackend"
```

`PlatformEmailBackend` reads the configuration **on every send** and dispatches:

```
send_mail() / EmailMultiAlternatives.send()
        │
        ▼
PlatformEmailBackend.send_messages()
        │
        ├─ EMAIL_PROVIDER=azure ──▶ azure.communication.email.EmailClient.begin_send()
        │
        └─ EMAIL_PROVIDER=smtp  ──▶ Django's SMTP backend, with live host/port/credentials
```

Two consequences worth knowing:

- **No restart is needed** to change provider or credentials. Values saved in
  Settings → Email take effect on the next message.
- **The sender is stamped at send time.** Django fills `from_email` from
  `DEFAULT_FROM_EMAIL`, which is read once at boot; the backend replaces it with
  the currently configured `EMAIL_FROM` unless a caller set something else
  deliberately.

Before sending anything, the backend runs the same readiness check the Settings
page displays (`mailer.status()`), so the banner on that page cannot claim email
works while sends are quietly failing for a missing value.

## Configuration

Every value resolves through [`ConvAI/site_config.py`](Django_CMS/ConvAI/site_config.py)
in the usual order: **the Settings page overrides `.env`**, and a blank field in
Settings means "fall back to `.env`".

### Common

| Variable | Description |
| --- | --- |
| `EMAIL_PROVIDER` | `azure` or `smtp`. Blank disables sending. |
| `EMAIL_FROM` | Sender address. Must be a verified sender on the provider's domain. |
| `EMAIL_FROM_NAME` | Display name. Blank uses `BRAND_NAME`. **Azure ignores this** — see below. |
| `EMAIL_REPLY_TO` | Where replies go, if anywhere. Comma-separated for several. |

### Azure Communication Services

Requires the `azure-communication-email` package (already in
`requirements.txt`). Give **either** the whole connection string **or** the
endpoint and access key — the platform assembles a connection string from the
pair, so both shapes the Azure portal hands out are accepted.

| Variable | Description |
| --- | --- |
| `AZURE_EMAIL_CONNECTION_STRING` | `endpoint=https://<resource>.<region>.communication.azure.com/;accesskey=<key>` |
| `AZURE_EMAIL_ENDPOINT` | Used only when the connection string is blank. |
| `AZURE_EMAIL_ACCESS_KEY` | Used only when the connection string is blank. |

ACS takes a bare sender address. The name recipients see comes from the **sender
username configured on the domain in the Azure portal**, not from
`EMAIL_FROM_NAME`, which is why that setting has no effect under Azure.

The domain that owns `EMAIL_FROM` must be provisioned and linked to the
Communication Services resource. A message sent from an unlinked domain is
accepted by the SDK and then reported as `Failed` — the Settings page shows
Azure's own wording for it.

### SMTP

| Variable | Description |
| --- | --- |
| `SMTP_HOST` | Server host name. |
| `SMTP_PORT` | Blank uses 587 for STARTTLS and 465 for SSL/TLS. |
| `SMTP_USER` / `SMTP_PASSWORD` | Blank sends unauthenticated. |
| `SMTP_SECURITY` | `tls` (STARTTLS), `ssl`, or `none`. Blank means `tls`. |

Django's conventional names — `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`,
`EMAIL_HOST_PASSWORD` — are also read from `.env` as aliases. The `SMTP_*` names
are the primary ones because Django defines the `EMAIL_*` settings itself with
non-empty defaults (`localhost`, `25`), so resolving through them would report
SMTP as configured on an install where nobody configured it.

SMTP conversations happen inside a web request, so the connection carries a
20-second timeout; without one, a single unreachable host holds a worker.

## Sending a test email

Settings → Email → **Send test email**. It sends one message using the **stored**
configuration, so save any edits first. On failure the provider's own error is
shown, because that is the only thing that says what to fix.

## Password recovery

The flow is Django's, with this project's templates and provider. Routes live in
[`ConvAI/urls_web.py`](Django_CMS/ConvAI/urls_web.py) and the views in
[`ConvAI/views/account.py`](Django_CMS/ConvAI/views/account.py):

| Path | Name | What it does |
| --- | --- | --- |
| `/password-reset/` | `password_reset` | Ask for the address, send the link |
| `/password-reset/sent/` | `password_reset_done` | "Check your inbox" |
| `/reset/<uidb64>/<token>/` | `password_reset_confirm` | Choose the new password |
| `/reset/done/` | `password_reset_complete` | Done; sign in |

Two things the stock Django views get wrong on this install, and how they are
fixed:

- **The link's host.** `django.contrib.sites` is installed with `SITE_ID = 1`, so
  the stock view builds the link against whatever that row says — `example.com`
  on an install nobody edited it on, producing a mail whose only link is dead.
  `PasswordResetRequestView` passes `domain_override=request.get_host()`
  instead; `ALLOWED_HOSTS` has already vetted that value.
- **User enumeration.** The "sent" page is worded so it reads the same whether or
  not an account exists for the address.

Reset links expire after `PASSWORD_RESET_TIMEOUT` seconds (default three hours)
and work once.

A user can only recover a password if their account **has an e-mail address**.
Admins set it under Users → edit.

## Reminders by email instead of WhatsApp

`REMINDER_CHANNEL` (Settings → Messaging → *Meeting reminder channel*) decides
what the **Send reminder** button on a scheduled call does:

| Value | Behaviour |
| --- | --- |
| `whatsapp` (default) | The existing Twilio Content API template message. |
| `email` | A rendered reminder email with the date, time and — for an in-person meeting — the location. |

Email reminders go to the **caregiver's address**, falling back to the
**client's** — the caregiver first because they are who the call is arranged
with. Both are edited on the client's record. When neither has an address the
Send reminder button is not offered, the same way it is withheld when the
caregiver has no phone number on the WhatsApp channel.

Whichever channel carries it, the reminder is recorded as a `Message` row under
the conversation id `reminder-<meeting id>`, so it appears in the history either
way.

## Templates

Email bodies are Django templates under
[`ConvAI/templates/email/`](Django_CMS/ConvAI/templates/email/), each in two
parts — `.txt` and `.html` — rendered together by `mailer.render_email()`:

| Template | Used for |
| --- | --- |
| `_base.html` | Shell every HTML message extends |
| `test_message.*` | The Settings page's test email |
| `meeting_reminder.*` | Email meeting reminders |
| `registration/password_reset_email.*` | The password-reset link |

The HTML uses table layout and inline styles on purpose: mail clients strip
`<style>` blocks and support little of the cascade, so anything that has to
survive Outlook goes in a `style` attribute.

Every message is sent as both plain text and HTML. A text part is not optional
politeness — mail clients set to refuse HTML need it, and spam filters count a
missing one against the sender.
