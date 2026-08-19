# Conversational Care API — Python client

A small Python client for the Conversational Care platform API. It authenticates
with your personal API token and enforces the same permissions as the web app
(navigators act on their own clients; admins on everybody).

## 1. Get your API token

1. Sign in to the platform and open your **Profile** page.
2. Click **Generate API token**.
3. Copy the token immediately — it is shown only once. Generating a new one
   replaces (revokes) the previous token.

## 2. Install

Download the package and install it with pip:

```bash
wget https://YOUR-PLATFORM/client-sdk/download/ -O conversationalcare_api.zip
pip install ./conversationalcare_api.zip
```

(Replace `https://YOUR-PLATFORM` with your platform's address. The exact
commands, pre-filled with your platform URL, are shown on the **Settings → API
client** page.)

## 3. Use it

```python
from conversationalcare_api import Client

client = Client(base_url="https://YOUR-PLATFORM", token="YOUR-TOKEN")

# List the clients you can see (optionally filter by name / last name / phone)
for p in client.list_patients():
    print(p["id"], p["name"], p["lastname"], p["phone_number"])

# Search
client.list_patients(q="Fonseca")

# Schedule a meeting for a client
result = client.schedule_meeting(
    patient_id=10,
    scheduled_time_iso="2026-08-01T15:30:00Z",
    type=1,                # 0 Onboarding, 1 Regular, 2 Final, 3 Initial
    scheduled_protocol=2,  # optional protocol number
)
print(result)  # {"ok": True, "meeting": {...}} or {"ok": False, "detail": "..."}
```

## Notes

- The API must be enabled on the platform (`ENABLE_API`).
- All requests use the header `Authorization: Token <your-token>`.
- Errors raise `conversationalcare_api.ConversationalCareError`; permission and
  conflict responses (403 / 409) are returned as data so you can handle them.
