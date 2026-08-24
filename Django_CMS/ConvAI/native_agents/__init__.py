"""In-process ("native") LangGraph agents that ship with Conversational Care.

Unlike *remote* agents — which run on a separate LangGraph server addressed by
``host:port`` — native agents are compiled and executed inside the Django
process. They persist per-conversation state through a LangGraph **Postgres
checkpointer** keyed by ``conversation_id`` (the LangGraph ``thread_id``), so
message history survives across turns and process restarts.

Adding a native agent:
    1. Create a module here that builds a compiled LangGraph graph.
    2. Decorate its builder with ``@register("<native_key>")``. The builder
       receives a checkpointer and must return a compiled graph.
    3. Create an ``Agent`` row with ``kind="native"`` and the matching
       ``native_key`` (the seed migration does this for the bundled agents).
"""
from __future__ import annotations

import asyncio
import logging
import threading

from django.conf import settings

logger = logging.getLogger(__name__)

# native_key -> builder(checkpointer) -> compiled graph
_BUILDERS: dict[str, callable] = {}
_SETUP_DONE = False
_SETUP_LOCK = threading.Lock()


def register(key: str):
    """Register a native graph builder under ``key``."""
    def deco(fn):
        _BUILDERS[key] = fn
        return fn
    return deco


def _load_builders():
    """Import agent modules so their ``@register`` decorators run.

    Imports are guarded: a native agent whose optional dependencies are missing
    (e.g. link_worker) must not break the registry for the others.
    """
    from importlib import import_module
    for mod in ("loopback", "link_worker", "protocol_qa", "self_registration"):
        try:
            import_module(f"{__name__}.{mod}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Native agent module %r failed to import: %s", mod, exc)


def available_keys() -> list[str]:
    _load_builders()
    return sorted(_BUILDERS.keys())


def _conn_string() -> str:
    db = settings.DATABASES["default"]
    return (
        f"postgresql://{db['USER']}:{db['PASSWORD']}"
        f"@{db['HOST']}:{db['PORT']}/{db['NAME']}"
    )


async def _arun_graph(build_graph, thread_id: str, user_message: str,
                      configurable: dict | None = None) -> str:
    """Run any in-process graph with the shared Postgres checkpointer.

    ``build_graph(checkpointer)`` must return a compiled LangGraph graph whose
    state has a ``messages`` channel.
    """
    global _SETUP_DONE
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(_conn_string()) as checkpointer:
        # Create checkpointer tables once per process (idempotent DDL).
        if not _SETUP_DONE:
            with _SETUP_LOCK:
                if not _SETUP_DONE:
                    await checkpointer.setup()
                    _SETUP_DONE = True

        try:
            graph = build_graph(checkpointer)
            config = {"configurable": {"thread_id": str(thread_id), **(configurable or {})}}
            result = await graph.ainvoke(
                {"messages": [{"role": "user", "content": user_message}]},
                config,
            )
        except RuntimeError as exc:
            # e.g. an agent that isn't fully provisioned yet.
            logger.warning("In-process agent unavailable: %s", exc)
            return f"Sorry, this agent is unavailable right now: {exc}"

    last = result["messages"][-1]
    return getattr(last, "content", None) or (last.get("content") if isinstance(last, dict) else "")


def _run_sync(coro_factory, label: str) -> str:
    """Run an async in-process invocation to completion from sync Django views."""
    # Warm the SiteConfiguration cache from THIS sync thread. The graph builder
    # (make_llm → get_setting/get_bool → SiteConfiguration.load) otherwise does
    # sync ORM inside the async event loop, which raises SynchronousOnlyOperation.
    # That error is swallowed and silently yields empty credentials (the OpenAI
    # key becomes ""), producing "Illegal header value b'Bearer '". Priming the
    # cache here means those reads hit the cache (no ORM) inside the loop.
    try:
        from ..models import SiteConfiguration
        SiteConfiguration.load()
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not warm SiteConfiguration cache before %s", label)

    try:
        return asyncio.run(coro_factory())
    except Exception:
        logger.exception("%s failed", label)
        return "Sorry, something went wrong generating the response."


def run_native(native_key: str, thread_id: str, user_message: str,
               configurable: dict | None = None, model_name: str | None = None) -> str:
    """Run a built-in native agent (selected by ``native_key``).

    ``model_name`` (from the ``Agent.model`` field) selects the LLM behind the
    agent; builders that don't use an LLM (e.g. loopback) ignore it.
    """
    _load_builders()
    builder = _BUILDERS.get(native_key)
    if builder is None:
        return f"Sorry, the native agent '{native_key}' is not available."
    return _run_sync(
        lambda: _arun_graph(
            lambda cp: builder(cp, model_name), thread_id, user_message, configurable,
        ),
        f"Native agent {native_key!r}",
    )


def run_prompt_agent(agent, thread_id: str, user_message: str,
                     configurable: dict | None = None, model_name: str | None = None) -> str:
    """Run a user-created prompt-based agent using its stored system prompt.

    Takes the ``Agent`` row rather than just the prompt string, because the RAG
    subtype also needs the agent's id (to scope the knowledge base) and its
    ``rag_top_k``.
    """
    from .prompt_agent import build_prompt_graph
    rag_enabled = bool(getattr(agent, "rag_enabled", False))
    return _run_sync(
        lambda: _arun_graph(
            lambda cp: build_prompt_graph(
                agent.system_prompt, cp, model_name,
                agent_id=agent.pk, rag_enabled=rag_enabled,
                top_k=getattr(agent, "rag_top_k", 5) or 5,
            ),
            thread_id, user_message, configurable,
        ),
        "RAG agent" if rag_enabled else "Prompt agent",
    )
