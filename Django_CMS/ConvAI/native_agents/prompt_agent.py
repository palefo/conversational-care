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

Both subtypes share the prompt and the model; the only difference is the tool.
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


def _build_rag_graph(system_prompt: str, checkpointer, model_name: str | None,
                     agent_id: int, top_k: int):
    """React agent whose one tool searches this agent's knowledge base."""
    from pydantic import BaseModel, Field
    from langchain_core.tools import tool
    from langgraph.prebuilt import create_react_agent

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

    full_prompt = (system_prompt or "").strip() + RAG_PROMPT_SUFFIX

    def prompt(state, config):
        msgs = state["messages"] if state.get("messages") else []
        return [{"role": "system", "content": full_prompt}] + list(msgs)

    model = make_llm(model_name, temperature=TEMPERATURE)
    # NOTE: this LangGraph (0.2.x) exposes the system-prompt hook as
    # ``state_modifier``; the newer ``prompt`` kwarg does not exist yet.
    return create_react_agent(
        model=model,
        tools=[search_documents],
        state_modifier=prompt,
        checkpointer=checkpointer,
    )


def build_prompt_graph(system_prompt: str, checkpointer, model_name: str | None = None,
                       agent_id: int | None = None, rag_enabled: bool = False,
                       top_k: int = 5):
    """Compile the graph for one prompt-based agent.

    ``model_name`` selects the model (blank → platform default); see
    ``ConvAI.llm_factory``. ``rag_enabled`` picks the subtype: with it on, the
    agent gets the ``search_documents`` tool over ``agent_id``'s documents.
    """
    if rag_enabled and agent_id:
        return _build_rag_graph(system_prompt, checkpointer, model_name, agent_id, top_k)
    return _build_plain_graph(system_prompt, checkpointer, model_name)
