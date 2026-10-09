"""Online meetings: browser rooms between a navigator and a client, with an
optional live voice agent and per-speaker audio recording.

An optional app. It plugs into the core only through ``ConvAI.extensions``, so
it can be switched off in Settings → Online meetings, left out of a deployment
with ``MEETINGS_APP=0``, or deleted, without the core noticing. See
online_meetings.md at the repository root.
"""
