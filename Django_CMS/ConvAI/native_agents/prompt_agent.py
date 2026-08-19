"""Prompt-based agents.

A *custom, configurable* agent kind: users create instances in the app, each
storing its own system prompt (`Agent.system_prompt`). The agent runs in-process
like the native agents (async LangGraph graph + Postgres checkpointer), but its
behaviour is driven entirely by the stored prompt — **no tools**.

It's a trimmed version of the "Reco A" sample (a react agent with a system
prompt): here we use a single model-call node instead of a tool-using react
agent, and read the prompt from the DB instead of a file.
"""
from __future__ import annotations

import os
from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

from ..llm_factory import make_llm

TEMPERATURE = float(os.getenv("PROMPT_AGENT_TEMPERATURE", "0"))


class PromptState(TypedDict):
    messages: Annotated[list, add_messages]


def build_prompt_graph(system_prompt: str, checkpointer, model_name: str | None = None):
    """Compile a single-node graph that prepends ``system_prompt`` and calls the LLM.

    ``model_name`` selects the model (blank → platform default); see
    ``ConvAI.llm_factory``.
    """
    model = make_llm(model_name, temperature=TEMPERATURE)
    system_prompt = (system_prompt or "").strip()

    async def call_model(state: PromptState) -> dict:
        msgs = state["messages"]
        if system_prompt:
            msgs = [{"role": "system", "content": system_prompt}] + list(msgs)
        response = await model.ainvoke(msgs)
        return {"messages": [response]}

    graph = StateGraph(PromptState)
    graph.add_node("call_model", call_model)
    graph.add_edge(START, "call_model")
    graph.add_edge("call_model", END)
    return graph.compile(checkpointer=checkpointer)
