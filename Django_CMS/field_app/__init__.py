"""Field App integration (optional app).

Connects Conversational Care to one or more Field App installations — the
tablet app link workers use on home visits — so an agent can be given, as
context, the Field App record of the client a conversation is with.

- Settings → Third-party apps: register a Field App installation.
- Profile → Field App: each link worker connects their own Field App account
  (consent screen in Field App; read-only token, stored here).
- Client page → Field App: link this client to their Field App athlete.
- Agent form → Data sources: switch the Field App record on for an agent.

The core only reaches this app through ConvAI.extensions. Leave it out with
FIELD_APP_INTEGRATION=0. See field_app_integration.md.
"""
