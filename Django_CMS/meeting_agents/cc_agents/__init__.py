"""Agents for Conversational Care online meetings.

* ``cc_agents.scribe`` — the recorder: one clean audio file per speaker.
* ``cc_agents.voice``  — the voice interviewer and the push-to-talk assistant.

Each is a LiveKit Agents worker registered under its own agent name
(``cc-scribe`` / ``cc-voice``), dispatched into a room explicitly by the
platform. They talk to the platform only through ``cc_agents.api``.
"""
