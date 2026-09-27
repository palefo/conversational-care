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

### In a remote agent (LangGraph)

A remote agent with **Allow callbacks** switched on in Conversational Care gets a
`cc_run_token` in every run's config. It names that one conversation, lets the
agent report its summary and — when the client asks — hide it from their link
worker, and expires after two hours. Nothing else accepts it.

The quickest way to use it is the ready-made tools:

```python
from conversationalcare_api.langgraph_tools import build_cc_tools, PRIVACY_PROMPT

graph = create_react_agent(
    model=model,
    tools=[get_current_date, *build_cc_tools()],
    prompt=lambda state, config: [{"role": "system",
                                   "content": BASE_PROMPT + PRIVACY_PROMPT}]
                                 + state["messages"],
)
```

`report_summary`, `get_conversation_privacy` and `set_conversation_privacy` take
**no conversation or client id** — the token says which conversation — and
reply "not connected" instead of failing when a run carries no token
(`langgraph dev`, tests, callbacks switched off).

`run_client.py` and `langgraph_tools.py` need only `requests` and
`langchain_core`, so an agent server can copy just those two files. For your
own tools, use the client directly:

```python
from conversationalcare_api import RunClient

client = RunClient.from_config(config)      # None when there is no token
if client:
    client.report_summary("What they wanted, what was said, what is still open.")
    client.set_visibility(hidden=True)       # only on the client's own say-so
```

The API address comes from `cc_api_url` in the run config, else
`CONVERSATIONAL_CARE_BASE_URL`. **Do not give an agent a personal API token for
this**: it is that person's access to every client, and the agent server stores
its run config.

### Conversations you hold yourself

With your own token you can read and set the summary and visibility of your own
conversations (for example ones you started through `/api/v1/messages/`):

```python
client.report_conversation_summary(conversation_id, "…")
client.set_conversation_visibility(conversation_id, hidden=True)
client.get_conversation_summary(conversation_id)
```

These return `None` when the conversation is not yours, does not exist, or the
feature is off — the API answers 404 to all three on purpose.

## Notes

- The API must be enabled on the platform (`ENABLE_API`).
- All requests use the header `Authorization: Token <your-token>`.
- Errors raise `conversationalcare_api.ConversationalCareError`; permission and
  conflict responses (403 / 409) are returned as data so you can handle them.
