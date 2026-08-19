"""Loopback native agent.

Uses LangGraph (but not as a LangGraph server): a single-node graph that replies
with exactly whatever it received as input. Useful as a connectivity/plumbing
test and as the reference implementation for native agents — it exercises the
Postgres checkpointer so conversation history accumulates per ``thread_id``.
"""
from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

from . import register


class LoopbackState(TypedDict):
    messages: Annotated[list, add_messages]


def _echo(state: LoopbackState) -> dict:
    last = state["messages"][-1]
    content = getattr(last, "content", None)
    if content is None and isinstance(last, dict):
        content = last.get("content", "")
    return {"messages": [{"role": "assistant", "content": content or ""}]}


@register("loopback")
def build(checkpointer, model_name=None):
    # Loopback echoes its input and uses no LLM, so ``model_name`` is ignored.
    graph = StateGraph(LoopbackState)
    graph.add_node("echo", _echo)
    graph.add_edge(START, "echo")
    graph.add_edge("echo", END)
    return graph.compile(checkpointer=checkpointer)
