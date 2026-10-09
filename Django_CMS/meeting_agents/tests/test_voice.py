"""Voice agent wiring that can be checked without a meeting server."""
import asyncio
import json

import pytest
from livekit.agents import Agent

from cc_agents import voice
from cc_agents.api import PlatformAPI


class _Run:
    cfg = {"instructions": "Base.", "protocol": {"title": "IQCODE"}, "respondent_name": "Ana",
           "client_first_name": "Manuel", "navigator_name": "Sam", "language": "pt-br"}

    def instructions(self):
        return voice.InterviewRun.instructions(self)


def test_interviewer_has_exactly_its_tools():
    agent = voice.InterviewerAgent(_Run())
    names = sorted(t.info.name for t in agent.tools)
    assert names == ["finish_interview", "flag_for_navigator", "get_current_date",
                     "get_protocol_questions", "save_protocol_answer"]


def test_interviewer_instructions_name_the_meeting():
    text = voice.InterviewRun.instructions(_Run())
    assert "IQCODE" in text and "Ana" in text and "Manuel" in text and "Portuguese" in text


def test_assistant_search_tool_only_when_asked():
    plain = Agent(instructions="x", tools=[])
    assert plain.tools == []
    searching = Agent(instructions="x", tools=[voice._search_tool(PlatformAPI("http://p", "t", "k"))])
    assert [t.info.name for t in searching.tools] == ["search_documents"]


def test_only_staff_may_steer():
    assert voice._staff("staff-12")
    assert not voice._staff("inv-abc")
    assert not voice._staff("agent-interviewer-1")
    assert not voice._staff("")


def test_model_is_built_for_azure_without_connecting():
    rt = {"endpoint": "https://example.openai.azure.com", "api_key": "k", "deployment": "gpt-realtime",
          "voice": "marin"}
    voice.make_model(rt)
    voice.make_model(rt, manual_turns=True)


def test_metadata_parsing():
    api, meta = PlatformAPI.from_metadata(json.dumps({"api_base": "http://web:8000", "token": "abc", "role": "scribe"}))
    assert api.token == "abc" and meta["role"] == "scribe"
    assert api.base.endswith("web:8000")
