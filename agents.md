# Agents

Conversational Care talks to its users through **agents**. Every chat surface —
the external chat, the navigator chatbot bubble, WhatsApp/SMS, protocol replies —
resolves an `Agent` and asks it to generate a response. Agents come in three
**kinds**, distinguished by the `Agent.kind` field.

## Concepts

- An **`Agent`** row (`ConvAI.models.Agent`) describes one assistant: its name,
  its kind, and the connection/identity details for that kind.
- A **`Patient`** (and, for the navigator bubble, a **user**) points at an agent
  via its `.agent` foreign key. That is the agent used for their conversations.
- Every response goes through **`generate_response_with_agent(agent, …)`** in
  `ConvAI/utils.py`, which branches on `agent.kind`. `generate_response_langgraph`
  is a thin wrapper that reads `.agent` off the patient/user and delegates to it.
- Conversations are keyed by a **`conversation_id`** (the LangGraph `thread_id`),
  so history is scoped per conversation regardless of kind.

In the app UI (**Agents** page, admin only) agents are grouped by kind in tabs:
**Prompt-based** and **Remote** are fully user-manageable (create / edit /
delete); **Native** ship with the platform, so they can't be created or deleted,
but admins **can edit** the model and TTS voice behind them. Every agent has a
**Test** button that opens the chat UI wired to just that agent (nothing is saved).

## The three kinds

### Remote agents (`kind = "remote"`)

The original model. A remote agent runs on a **separate LangGraph server** and is
addressed by `host` + `port` + `langgraph_name`. The platform connects to it with
`RemoteGraph` (from `langgraph_sdk`) and invokes it over HTTP. The LangGraph
server owns the model, the tools, and its own state persistence.

Remote agents are created/edited like any other record (app-level `AgentForm` or
the Django admin). Requests are restricted to allow-listed hosts (SSRF guard in
`agent_host_allowed`).

Use a remote agent when the logic lives in a deployed LangGraph app you manage
outside this repo.

### Prompt-based agents (`kind = "prompt"`)

A **custom, configurable** kind: admins create instances in the app, each storing
its own **system prompt** in the DB (`Agent.system_prompt`). It runs in-process
like a native agent (async graph + Postgres checkpointer) but is a single
model-call node with the stored prompt as the system message and **no tools**.
It's a trimmed version of the "Reco A" sample (`ConvAI/native_agents/prompt_agent.py`).

The model is chosen per agent via **`Agent.model`** and built by the shared
factory (see *Choosing the model* below). It needs the provider credentials **in
the Django container** (remote agents don't, because the LLM call happens on their
server); without them a prompt-based agent returns a clear "unavailable" message.

**Real-time voice** (`Agent.realtime_enabled`): a prompt-based agent can instead
converse speech-to-speech over the **Azure OpenAI GPT Realtime** API. The agent
test page and the tester chat then render a live voice-call UI
(`chat/realtime_voice.html`) with start/stop and mute instead of the text chat.
The browser connects to Azure directly over WebRTC, authorised by a short-lived
ephemeral key minted server-side (`ConvAI/realtime.py`) only when the call is
started — the Azure API key never reaches the client and the Realtime API is not
touched by merely opening the page; calls also auto-end after ~2 minutes of
silence. `Agent.model` is ignored in this mode: the deployment comes from
`AZURE_REALTIME_DEPLOYMENT` (with `AZURE_REALTIME_ENDPOINT` / `_API_KEY` falling
back to the Azure OpenAI values, and `AZURE_REALTIME_VOICE` for the voice), all
overridable at runtime in **Settings → Agents → Azure OpenAI Realtime**.
Realtime conversations are not persisted as `Message` rows.

The realtime models (`gpt-realtime`, `gpt-realtime-mini`) are only deployable in
a few Azure regions (East US 2, Sweden Central), so they typically live on a
dedicated resource — hence the separate endpoint/key settings. Resources that
don't expose the GA v1 realtime surface (it 404s) are handled automatically via
the *preview* API fallback, which additionally needs
`AZURE_REALTIME_WEBRTC_REGION` (the resource's region, e.g. `swedencentral`)
because the preview WebRTC gateway is regional.

### Native agents (`kind = "native"`)

Native agents **ship with the platform** and run **in-process** inside Django —
no separate server. They live in `ConvAI/native_agents/` and are selected by
`Agent.native_key`. `host` / `port` / `langgraph_name` are unused for them.

Native agents:

- are compiled LangGraph graphs invoked **asynchronously** (`ainvoke`, run to
  completion by `run_native`);
- **persist per-conversation state** via a LangGraph **Postgres checkpointer**
  (`AsyncPostgresSaver`) keyed by `conversation_id`, so history survives across
  turns and restarts;
- register themselves in `ConvAI/native_agents/__init__.py` via `@register(key)`.

The bundled native agents are seeded by a data migration
(`0044_seed_native_agents`):

| `native_key`  | Name        | What it does |
|---------------|-------------|--------------|
| `loopback`    | Loopback    | Uses LangGraph but replies with **exactly** what it received. A connectivity/plumbing test and the reference implementation for native agents. |
| `link_worker` | Link Worker | The agent behind the **navigator chatbot bubble**. A `create_react_agent` that can search patients and schedule meetings. |

> **Link Worker is scaffolded.** The framework, dispatch, and persistence around
> it are complete, but it returns a friendly "agent unavailable" message until the
> Django image is provisioned with the `conversationalcare_api` package and the
> credentials for its selected model. See the note at the top of
> `ConvAI/native_agents/link_worker.py`.

## Choosing the model

In-process agents (native + prompt-based) pick their LLM through one shared
factory, **`ConvAI/llm_factory.py`** (`make_llm`). Remote agents don't use it —
their server owns the model.

- **Per agent.** Each agent's **`Agent.model`** field selects the model. Admins
  set it in the Agents UI (a free-text field with common suggestions). Blank uses
  the platform default, **`DEFAULT_AGENT_MODEL`** (falls back to `gpt-4.1-mini`).
- **Provider is inferred** from the string, or set explicitly with a prefix:
  `openai/…`, `anthropic/…`, `google/…`, `mistral/…`, `deepseek/…` (e.g.
  `anthropic/claude-sonnet-4-6`). Anything unprefixed is treated as OpenAI.
- **API keys** come from `.env` — `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
  `GOOGLE_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY`. Only the providers you
  use are required. If a provider's SDK isn't installed, selecting it yields a
  clear "add `langchain-…`" message instead of a crash.
- **Editable in the app.** The default model, the provider keys, and all the
  Azure settings can also be set at runtime in **Settings → Agents** (stored on
  `SiteConfiguration`). These DB values take precedence over `.env`, so you can
  switch models/providers without redeploying. Keys are write-only (a blank field
  keeps the stored secret). Resolution order is DB → `.env` → Django settings.
- **Azure.** Set `USE_AZURE=true` to route models to Azure instead of the public
  APIs — Azure OpenAI, Azure Anthropic, and Azure Mistral/DeepSeek (OpenAI-
  compatible) each have their own endpoint/key vars. Under Azure OpenAI the model
  string is the **deployment name**. See `.env.sample` for the full list.

## Which surface uses which agent

- **External chat / voice** (`external_chat`, `send_external_message`,
  `process_audio`) uses the linked patient's `.agent`. For the test user this is
  typically **Loopback**.
- **Navigator chatbot bubble** (`send_chat_message`) always uses the built-in
  **Link Worker** native agent and keys the conversation by a normalized UUID
  derived from the `conversation_id`.

## Adding a new native agent

1. Create a module in `ConvAI/native_agents/` whose builder returns a compiled
   LangGraph graph, and decorate it with `@register("your_key")`. The builder
   receives the checkpointer — pass it to `.compile(checkpointer=…)` (or to
   `create_react_agent(checkpointer=…)`) so state is persisted.
2. Keep heavy or optional imports **inside** the builder so a missing dependency
   can't break the registry for the other agents.
3. Add an `Agent` row with `kind="native"` and the matching `native_key`
   (a data migration, like `0044`, is the clean way to ship it).

Dependencies for the Postgres checkpointer are pinned in `requirements.txt`
(`langgraph-checkpoint` / `langgraph-checkpoint-postgres`) for compatibility with
the project's LangGraph version.

## Roadmap

Per-agent model selection and multi-provider (incl. Azure) routing are in place.
Natural extensions: per-agent temperature, and optional tools for prompt-based
agents.
