"""LLM factory for in-process (native + prompt-based) agents.

Builds a LangChain chat model from a (optionally provider-prefixed) model string,
so an admin can choose the model behind an agent (e.g. ``openai/gpt-4.1-mini``,
``anthropic/claude-sonnet-4-6``, ``mistral/mistral-large-latest``, ...).

Provider is inferred from the string (or an explicit ``provider/`` prefix). When
``USE_AZURE=true`` the same string is routed to the matching Azure deployment
instead of the public API, so no per-agent change is needed to switch a whole
deployment between direct and Azure. See agents.md and .env.sample.

The returned models are LangChain chat models and support both ``.invoke`` and
``.ainvoke`` — the in-process agents call them asynchronously.
"""
from __future__ import annotations

from langchain_openai import (ChatOpenAI, AzureChatOpenAI,
                              OpenAIEmbeddings, AzureOpenAIEmbeddings)

from .site_config import get_setting, get_bool

try:
    from langchain_anthropic import ChatAnthropic
except Exception:  # pragma: no cover - optional provider
    ChatAnthropic = None
try:
    from langchain_google_genai import ChatGoogleGenerativeAI
except Exception:  # pragma: no cover - optional provider
    ChatGoogleGenerativeAI = None
try:
    from langchain_mistralai import ChatMistralAI
except Exception:  # pragma: no cover - optional provider
    ChatMistralAI = None


# ---------------------------------------------------------------------------
# Defaults / Azure config.
#
# Values are resolved through ``site_config`` so an admin can override them in
# the Settings → Agents page (DB), falling back to .env, then Django settings.
# ---------------------------------------------------------------------------

def _s(key: str):
    """Resolved string setting (DB → env → settings), or None if unset/blank."""
    return get_setting(key) or None


def default_model() -> str:
    """Platform default model, used when an agent has no explicit model set."""
    return _s("DEFAULT_AGENT_MODEL") or _s("OPENAI_MODEL") or "gpt-4.1-mini"


def _use_azure() -> bool:
    return get_bool("USE_AZURE", False)


# ---------------------------------------------------------------------------
# Provider handling
# ---------------------------------------------------------------------------
_PREFIXES = (
    "openai/", "openai:", "anthropic/", "anthropic:", "google/", "google:",
    "gemini/", "gemini:", "mistral/", "mistral:", "deepseek/", "deepseek:",
)


def _strip_provider_prefix(model_name: str) -> str:
    m = (model_name or "").strip()
    for pfx in _PREFIXES:
        if m.lower().startswith(pfx):
            return m[len(pfx):].strip()
    return m


def _infer_provider(model_name: str) -> str:
    m = (model_name or "").strip().lower()
    if m.startswith(("anthropic/", "anthropic:")) or "claude" in m:
        return "anthropic"
    if m.startswith(("google/", "google:", "gemini/", "gemini:")) or m.startswith("gemini"):
        return "google"
    if m.startswith(("mistral/", "mistral:")) or m.startswith(("mistral", "open-mistral")):
        return "mistral"
    if m.startswith(("deepseek/", "deepseek:")) or "deepseek" in m:
        return "deepseek"
    return "openai"


def make_llm(model_name: str | None = None, temperature: float = 0.0):
    """Build a LangChain chat model for ``model_name`` (blank → platform default)."""
    model_name = (model_name or "").strip() or default_model()
    provider = _infer_provider(model_name)
    clean_model = _strip_provider_prefix(model_name)
    use_azure = _use_azure()

    if provider == "anthropic":
        if ChatAnthropic is None:
            raise RuntimeError(
                "Anthropic support is not installed. Add `langchain-anthropic` "
                "and set ANTHROPIC_API_KEY (or the Azure Anthropic variables)."
            )
        if use_azure:
            # Azure Anthropic — Anthropic fixes temperature at 1.0, so omit it.
            return ChatAnthropic(
                model=clean_model,
                anthropic_api_key=_s("AZURE_ANTHROPIC_API_KEY"),
                anthropic_api_url=_s("AZURE_ANTHROPIC_ENDPOINT"),
            )
        return ChatAnthropic(
            model=clean_model, temperature=temperature,
            anthropic_api_key=_s("ANTHROPIC_API_KEY"),
        )

    if provider == "google":
        if ChatGoogleGenerativeAI is None:
            raise RuntimeError(
                "Google support is not installed. Add `langchain-google-genai` "
                "and set GOOGLE_API_KEY."
            )
        # Google has no Azure path — always the direct API.
        return ChatGoogleGenerativeAI(
            model=clean_model, temperature=temperature,
            google_api_key=_s("GOOGLE_API_KEY"),
        )

    if provider == "mistral":
        if use_azure:
            # Azure Mistral exposes an OpenAI-compatible endpoint.
            return ChatOpenAI(
                model=clean_model,
                base_url=_s("AZURE_MISTRAL_ENDPOINT"),
                api_key=_s("AZURE_MISTRAL_API_KEY"),
                temperature=temperature,
            )
        if ChatMistralAI is None:
            raise RuntimeError(
                "Mistral support is not installed. Add `langchain-mistralai` "
                "and set MISTRAL_API_KEY."
            )
        return ChatMistralAI(
            model=clean_model,
            temperature=temperature,
            mistral_api_key=_s("MISTRAL_API_KEY"),
        )

    if provider == "deepseek":
        if use_azure:
            # DeepSeek shares the Azure AI Foundry (OpenAI-compatible) resource
            # with Mistral; fall back to the Mistral endpoint/key if unset.
            base_url = _s("AZURE_DEEPSEEK_ENDPOINT") or _s("AZURE_MISTRAL_ENDPOINT")
            api_key = _s("AZURE_DEEPSEEK_API_KEY") or _s("AZURE_MISTRAL_API_KEY")
            return ChatOpenAI(model=clean_model, base_url=base_url, api_key=api_key, temperature=temperature)
        return ChatOpenAI(
            model=clean_model,
            base_url=_s("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
            api_key=_s("DEEPSEEK_API_KEY"),
            temperature=temperature,
        )

    # Default: OpenAI
    if use_azure:
        # For Azure OpenAI, the model string is the *deployment* name.
        return AzureChatOpenAI(
            azure_endpoint=_s("AZURE_OPENAI_ENDPOINT"),
            azure_deployment=clean_model,
            api_key=_s("AZURE_OPENAI_API_KEY"),
            api_version=_s("AZURE_OPENAI_API_VERSION") or "2024-12-01-preview",
            temperature=temperature,
        )
    return ChatOpenAI(model=clean_model, temperature=temperature, api_key=_s("OPENAI_API_KEY"))


# ---------------------------------------------------------------------------
# Embeddings (RAG-based prompt agents)
#
# Only OpenAI/Azure OpenAI here. The embedding model is what fixes the vector
# space a knowledge base lives in, so it is a *platform* setting rather than a
# per-agent one — changing it per agent would silently make two agents' vectors
# incomparable for no benefit.
#
# `text-embedding-3-small` is the default because it is multilingual (the same
# vector space across languages, so a Spanish question finds the answer in an
# English document) and the cheapest of the three.
# ---------------------------------------------------------------------------
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


def embedding_model_name() -> str:
    """The embedding model used to build and query every knowledge base."""
    return _s("RAG_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL


def make_embeddings(model_name: str | None = None):
    """Build the LangChain embeddings client (OpenAI, or Azure under USE_AZURE).

    Returns an object with ``embed_documents(list[str])`` and
    ``embed_query(str)``. Raises ``RuntimeError`` when the credentials for the
    selected route are missing, so callers can surface one clear message
    instead of an opaque auth error mid-ingest.
    """
    model_name = (model_name or "").strip() or embedding_model_name()
    model_name = _strip_provider_prefix(model_name)

    if _use_azure():
        # Under Azure the model string is a *deployment* name; the deployment
        # setting wins, and the model name is a reasonable fallback because
        # deployments are conventionally named after their model.
        endpoint = _s("AZURE_OPENAI_ENDPOINT")
        api_key = _s("AZURE_OPENAI_API_KEY")
        if not endpoint or not api_key:
            raise RuntimeError(
                "Azure OpenAI is not configured. Set the endpoint and key under "
                "Settings → Agents → Azure before uploading documents."
            )
        return AzureOpenAIEmbeddings(
            azure_endpoint=endpoint,
            azure_deployment=_s("AZURE_EMBEDDING_DEPLOYMENT") or model_name,
            api_key=api_key,
            api_version=_s("AZURE_OPENAI_API_VERSION") or "2024-12-01-preview",
        )

    api_key = _s("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it under Settings → Integrations "
            "before uploading documents."
        )
    return OpenAIEmbeddings(model=model_name, api_key=api_key)
