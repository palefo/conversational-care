# ConvAI — the Conversational Care application

`ConvAI` is the Django app behind the platform: the care-team web interface,
the REST API, the Twilio webhook, and the in-process AI agents.

Layout highlights:

- `views/` — one module per area (patients, calls, chat, alerts, settings, …),
  with shared role decorators re-exported from `views/_base.py`.
- `api/` — DRF views, serializers and throttles for `/api/v1/…`.
- `native_agents/` — in-process LangGraph agents (see [agents.md](../../agents.md)).
- `templates/` — server-rendered UI, one folder per area.
- `roles.py`, `site_config.py`, `llm_factory.py`, `async_reply.py` — the
  cross-cutting pieces, all documented in the
  [technical manual](../../docs/technical-manual.md).
