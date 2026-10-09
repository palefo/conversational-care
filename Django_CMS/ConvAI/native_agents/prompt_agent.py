"""Prompt-based agents, in two subtypes.

A *custom, configurable* agent kind: users create instances in the app, each
storing its own system prompt (`Agent.system_prompt`). The agent runs in-process
like the native agents (async LangGraph graph + Postgres checkpointer).

* **Plain** (`rag_enabled = False`) — a single model-call node. Behaviour is
  driven entirely by the stored prompt; no tools. A trimmed version of the
  "Reco A" sample, reading the prompt from the DB instead of a file.

* **RAG-based** (`rag_enabled = True`) — the same prompt, plus one tool,
  ``search_documents``, over the files uploaded against that agent. It is a
  react agent so the model *chooses* when to search: a greeting shouldn't cost
  an embedding call, and a follow-up question often should search again with
  different words. Retrieval never happens behind the model's back.

Either subtype may additionally be given **platform tools** — conversation
privacy, summary reporting — ticked on the agent form and listed in
``tool_registry``. Any tool at all means a react agent: a plain prompt agent is
a single model-call node with no tool loop, so a tool handed to it would simply
never be called. The upgrade happens here rather than being a switch on the
form, because "you must also tick this other box" is a question with one correct
answer.

Each tool contributes wording to the system prompt, appended after the agent's
own and in registry order, the same way ``RAG_PROMPT_SUFFIX`` already is.
"""
from __future__ import annotations

import logging
import os
from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

from ..llm_factory import make_llm

logger = logging.getLogger(__name__)

TEMPERATURE = float(os.getenv("PROMPT_AGENT_TEMPERATURE", "0"))

# Appended to the agent's own prompt when RAG is on. It says three things the
# stored prompt shouldn't have to repeat: search before answering, answer from
# what came back, and stay in the user's language — the knowledge base is
# multilingual (one embedding space across languages), so a Spanish question
# routinely retrieves English extracts and the reply must not follow them into
# English.
RAG_PROMPT_SUFFIX = """

## Knowledge base

You have a `search_documents` tool over documents provided by this service.

- Search it whenever the user asks something those documents might cover,
  before answering. Search again with different wording if the first result
  is thin.
- Base your answer on what the search returns, and say which document it came
  from. Do not add facts the extracts do not support.
- If the search finds nothing relevant, say you don't have that information.
  Never invent it.
- The documents may be in a different language from the conversation. Always
  reply in the language the user is writing in, translating what you found."""


class PromptState(TypedDict):
    messages: Annotated[list, add_messages]


def _build_plain_graph(system_prompt: str, checkpointer, model_name: str | None):
    """Single-node graph: prepend the system prompt, call the model."""
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


def _rag_tool(agent_id: int, top_k: int):
    """The ``search_documents`` tool over one agent's knowledge base."""
    from pydantic import BaseModel, Field
    from langchain_core.tools import tool

    from ..rag.retrieve import format_hits, search

    class SearchInput(BaseModel):
        query: str = Field(
            ...,
            description=("What to look for, in the user's own words. Full "
                         "questions work better than keywords. Use the "
                         "language the user wrote in — the search matches "
                         "across languages."),
        )

    # Synchronous on purpose. LangGraph runs sync tools in a worker thread
    # during ainvoke(), so Django ORM access here is safe — an async tool would
    # be executing inside the event loop, where the ORM raises
    # SynchronousOnlyOperation. (Same reasoning as link_worker's tools.)
    @tool("search_documents", args_schema=SearchInput)
    def search_documents(query: str) -> str:
        """Search this service's documents for passages relevant to a question.

        Returns the best-matching extracts, each labelled with the document it
        came from, or a note saying nothing relevant was found.
        """
        try:
            return format_hits(search(agent_id, query, top_k))
        except RuntimeError as exc:
            # Misconfiguration (missing key, changed embedding model). The
            # model should tell the user rather than answer from thin air.
            return f"The knowledge base is unavailable: {exc}"
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Knowledge-base search failed for agent %s", agent_id)
            return f"The knowledge base search failed: {exc}"

    return search_documents


def _build_react_graph(system_prompt: str, checkpointer, model_name: str | None,
                       tools: list):
    """React agent over ``tools``, with the assembled prompt as its system message."""
    from langgraph.prebuilt import create_react_agent

    full_prompt = (system_prompt or "").strip()

    def prompt(state, config):
        msgs = state["messages"] if state.get("messages") else []
        return [{"role": "system", "content": full_prompt}] + list(msgs)

    model = make_llm(model_name, temperature=TEMPERATURE)
    # NOTE: this LangGraph (0.2.x) exposes the system-prompt hook as
    # ``state_modifier``; the newer ``prompt`` kwarg does not exist yet.
    return create_react_agent(
        model=model,
        tools=tools,
        state_modifier=prompt,
        checkpointer=checkpointer,
    )


def build_prompt_graph(system_prompt: str, checkpointer, model_name: str | None = None,
                       agent_id: int | None = None, rag_enabled: bool = False,
                       top_k: int = 5, agent=None):
    """Compile the graph for one prompt-based agent.

    ``model_name`` selects the model (blank → platform default); see
    ``ConvAI.llm_factory``. ``rag_enabled`` adds the ``search_documents`` tool
    over ``agent_id``'s documents. ``agent`` is the row itself, needed for the
    platform tools ticked on it — passing it is what lets the registry stay the
    only place that knows which tools exist.

    The prompt is assembled here, in one place, so what the model is told about
    its tools cannot drift from which tools it was actually handed: a suffix is
    appended only alongside the tool it describes.
    """
    from .tool_registry import build_tools, prompt_suffix

    tools, prompt = [], (system_prompt or "").strip()

    if rag_enabled and agent_id:
        tools.append(_rag_tool(agent_id, top_k))
        prompt += RAG_PROMPT_SUFFIX

    if agent is not None:
        platform_tools = build_tools(agent)
        if platform_tools:
            tools.extend(platform_tools)
            prompt += prompt_suffix(agent)

    # No tools at all → the single-node graph. A react agent with an empty tool
    # list still pays for the scaffolding and can still emit an empty tool call.
    if not tools:
        return _build_plain_graph(prompt, checkpointer, model_name)
    return _build_react_graph(prompt, checkpointer, model_name, tools)
