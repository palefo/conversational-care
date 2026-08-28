# Agents

Conversational Care talks to its users through **agents**. Every chat surface —
the external chat, the navigator chatbot bubble, WhatsApp/SMS, protocol replies —
resolves an `Agent` and asks it to generate a response. Agents come in three
**kinds**, distinguished by the `Agent.kind` field.

## Concepts

- An **`Agent`** row (`ConvAI.models.Agent`) describes one assistant: its name,
  its kind, and the connection/identity details for that kind.
- **`Agent.description`** is one line on what the agent is *for*, in the admin's
  own words. It is shown on the agent's card on the Agents page, so the list
  reads as a roster instead of four names and a model id. Optional for every
  kind; native agents ship with one (seeded by migration `0082`). A card with no
  description falls back to the technical identity it always showed — the
  `native_key`, the LangGraph name, or the first line of the system prompt.
  The card gives it two lines and trims the rest — about 90 characters, which
  is the budget the seeded descriptions are held to (`CARD_BUDGET` in that
  migration). Longer text still reads in full by hovering the card.
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
but admins **can edit** their description, model and TTS voice. Every agent has a
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
like a native agent (async graph + Postgres checkpointer).

Prompt-based agents come in two **subtypes**, selected by `Agent.rag_enabled`:

| Subtype | What it is |
|---|---|
| **Plain** (`rag_enabled = False`) | A single model-call node with the stored prompt as the system message and **no tools**. A trimmed version of the "Reco A" sample (`ConvAI/native_agents/prompt_agent.py`). |
| **RAG-based** (`rag_enabled = True`) | The same prompt, plus one tool — `search_documents` — over files uploaded against that agent. See [RAG-based agents](#rag-based-agents-rag_enabled--true) below. |

Both share the model, the prompt, and the classification settings; the Agents
page lists them as two groups under the same **Prompt-based** tab.

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

#### RAG-based agents (`rag_enabled = True`)

A RAG agent answers from **documents you upload to it** rather than from the
model's own knowledge. Same stored prompt, same model — plus one tool.

Turning the toggle **off** does not touch the documents or their vectors. The
agent simply stops being handed the tool, and turning it back on costs nothing.

##### It is a tool, not a preprocessor

Retrieval is exposed as a LangGraph tool (`search_documents`) on a
`create_react_agent`, so the **model decides when to search**. That matters in
practice: a greeting shouldn't cost an embedding call, and a follow-up question
often needs a second search with different words. Nothing is silently stuffed
into the prompt behind the model's back, so what it saw is always visible in
the trace.

The tool is deliberately **synchronous**. LangGraph runs sync tools in a worker
thread during `ainvoke()`, so Django ORM access inside them is safe; an async
tool would execute inside the event loop, where the ORM raises
`SynchronousOnlyOperation`. (Same reasoning as `link_worker`'s tools.)

`Agent.rag_top_k` (default 5) sets how many extracts one search returns.

##### Multilingual by construction

The default embedding model, `text-embedding-3-small`, puts all languages in
**one** vector space, so a question asked in Spanish retrieves the passage that
answers it even when the document is in English. The prompt suffix
(`prompt_agent.RAG_PROMPT_SUFFIX`) then tells the agent to reply in the
language the user wrote in, translating what it found — without that
instruction models drift into the language of the extracts.

Text extraction is language-aware too: `.txt`/`.md` files are decoded as UTF-8
and, failing that, sniffed with `chardet` before falling back to latin-1, so a
cp1252 export does not arrive with its accents mangled. Chunking is by
characters rather than tokens, so a Chinese or Korean document is split the
same way an English one is.

##### The lightweight architecture

**There is no vector database and no extra service.** A chunk's vector is
stored on its own row as a packed little-endian float32 blob — 4 bytes a
dimension, ~6 KB for a 1536-dim vector against ~24 KB as JSON — L2-normalised
on the way in, so cosine similarity at query time is a plain dot product.
Retrieval stacks one agent's vectors into a numpy matrix and scores them in a
single multiply.

That is the right shape for this workload. A knowledge base here is a handful
of documents — hundreds to a few thousand chunks — and scoring 5,000 × 1536
floats takes a couple of milliseconds, which rounds to nothing beside the
embedding call that had to happen first. The matrix is cached per process and
keyed by a cheap fingerprint of the agent's ready-and-enabled documents, so it
is rebuilt only when the set actually changes.

It also keeps **one** source of truth. Switching a document off is a `WHERE`
clause, not an index rebuild — which is exactly why the off toggle can be free.

If a deployment ever outgrows this, the change is local: keep the schema and
put an ANN index in front of `retrieve._matrix_for`.

| Model | What it holds |
|---|---|
| `RagDocument` | One uploaded file: its name, size, on/off flag, ingestion status and progress, and the embedding model its vectors were built with. |
| `RagChunk` | One embedded slice of a document: ordinal, text, and the packed vector. Cascades with the document. |

##### Ingestion, and why a reload is harmless

Uploading is one HTTP request **per file** — that is what gives each file its
own progress bar, and stops one rejected file from failing the whole drop. The
request saves the bytes, creates the row and returns; the work happens on the
ingestion pool (`async_reply.submit_ingest`, sized by `RAG_WORKERS`, separate
from the WhatsApp reply pool so a long PDF cannot delay a reply).

Every bit of job state lives on the `RagDocument` row — stage, chunks done,
error. The page is only ever a *view* onto those rows, never the owner of the
job, so closing the tab, reloading, or coming back on another machine all show
the same progress.

The harder case is a process restart mid-ingest. `updated_at` is written on
every progress tick, so it doubles as the worker's heartbeat: a row claiming to
be in-flight that has not been touched for five minutes has lost its worker.
`resume_orphans` re-queues those whenever anyone looks at the knowledge base,
and claiming is a single conditional `UPDATE`, so two web processes racing to
resume the same document cannot both win. Ingestion always rebuilds a
document's chunks from scratch, so a re-run is never additive.

Stages, as shown in the UI: `pending` → `extracting` → `chunking` →
`embedding` → `ready`, or `failed` with the reason and a **Retry** button.
Failures are usually transient or fixable elsewhere (a missing API key, a rate
limit part-way through embedding), and the file is already stored, so a retry
does not need a re-upload.

##### Files, formats and limits

| Format | Read with | Notes |
|---|---|---|
| `.txt`, `.md` | direct | UTF-8, then `chardet`, then latin-1. |
| `.pdf` | `pypdf` | Per-page; one unreadable page costs that page, not the upload. A scanned PDF with no text layer fails with a clear "needs OCR" message rather than indexing nothing. |
| `.docx` | `python-docx` | Tables are walked separately — `document.paragraphs` omits them, and in care documents tables carry a lot of the content. The older `.doc` format is not supported. |

Uploads are capped at 25 MB (`rag.extract.MAX_UPLOAD_BYTES`) and chunked at
1000 characters with a 150-character overlap (`rag.ingest`).

Stored files live under `media/rag_documents/<agent_id>/` with randomised
names. **Nothing serves them** — there is no download view; the file is written
once by the upload, read once by the worker, and kept only so a failed
ingestion can be retried. Deleting a document (or its agent) deletes the file.
See [file_storage.md](file_storage.md).

##### Configuration

The embedding model is a **platform** setting, not a per-agent one: it fixes
the vector space a knowledge base lives in, and letting agents differ would
silently make their vectors incomparable for no benefit.

| Setting | Default | What it does |
|---|---|---|
| `RAG_EMBEDDING_MODEL` | `text-embedding-3-small` | Model used to index and search. Also editable in **Settings → Agents → Embeddings**. |
| `AZURE_EMBEDDING_DEPLOYMENT` | — | Under `USE_AZURE`, the deployment serving that model. Blank reuses the model name; endpoint/key come from `AZURE_OPENAI_*`. |
| `RAG_WORKERS` | `2` | Background threads that read, chunk and embed. |

> **Under `USE_AZURE`, embeddings need their own deployment.** An Azure OpenAI
> resource does not serve `text-embedding-3-small` just because it serves a chat
> model — embeddings are a *separate* deployment you create on the resource, and
> its name is often not the model name. Without one, ingestion reaches the
> embedding step and fails with `DeploymentNotFound` (extraction and chunking
> having already succeeded). Create the deployment, put its exact name in
> **Settings → Agents → Embeddings**, and press **Retry** — the file is already
> stored, so nothing needs re-uploading.

> **Changing the embedding model invalidates existing documents.** Each
> document records the model its vectors were built with; a query embedded with
> a different model has a different width and cannot be compared. Rather than
> return nonsense, the search tool says the knowledge base was built with a
> different model and points at re-uploading or restoring the old setting.

A RAG agent cannot also be a **real-time voice** agent: realtime agents
converse straight with the voice API and never call the tool, so the form
rejects the combination rather than shipping a knowledge base that can never
be consulted.

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
- **Editable in the app.** The default model, the provider keys, the embedding
  model, and all the Azure settings can also be set at runtime in
  **Settings → Agents** (stored on `SiteConfiguration`). These DB values take precedence over `.env`, so you can
  switch models/providers without redeploying. Keys are write-only (a blank field
  keeps the stored secret). Resolution order is DB → `.env` → Django settings.
- **Azure.** Set `USE_AZURE=true` to route models to Azure instead of the public
  APIs — Azure OpenAI, Azure Anthropic, and Azure Mistral/DeepSeek (OpenAI-
  compatible) each have their own endpoint/key vars. Under Azure OpenAI the model
  string is the **deployment name**. See `.env.sample` for the full list.

## Classification and alerts

Every agent carries three optional classification settings, edited under
**Agents → edit → Advanced: conversation classification** (and mirrored in
Django admin):

| Field | What it does |
|---|---|
| `classification_role` | The persona the classifier adopts. Continues the phrase "You are …". |
| `abstract_instruction` | How the conversation summary should be written. |
| `detectors` | What this agent watches for, and what it does when it finds it. |

### Detectors

`Agent.detectors` is a JSON object keyed by detector label:

```json
{
  "Missed medication": {
    "instruction": "Mentions skipping or forgetting doses",
    "raises": true,
    "priority": 2
  },
  "Asks about services": {
    "instruction": "Questions about day centres, respite, benefits",
    "raises": false
  }
}
```

`priority` mirrors `Alert.Priority` (1 High, 2 Medium, 3 Low) and only matters
when `raises` is true. The older shape — `{"label": "instruction"}` — still
reads, and means *detect but do not raise*, which is what those rows did before
alerts existed; they are not silently promoted.

`ConvAI/forms.py:DetectorTableWidget` is the editor, shared by the Agents page
and Django admin so the two cannot drift.

### The self-harm floor

One detector is compiled in and applies to every agent, configured or not:
`SAFETY_DETECTOR` in `ConvAI/utils_conversation_classification.py`. An admin can
reword its instruction by using the same label; they cannot stop it raising or
lower it below High. The reasoning is in the code comment — a forgotten config
field must not be the only thing between a disclosure and a navigator.

### When classification runs

`process_message_for_patient` hands each inbound turn to
`conversation_alerts.review_conversation` on the **ingest** thread pool, so the
classifier never delays the caregiver's reply. Turns with no inbound text
(outbound reminders and templates) are skipped — no new evidence, no new
verdict. The Settings → **Classify conversations** button calls the same
function over anything unanalysed, as a backstop for conversations that predate
the hook or that the pool dropped.

`review_conversation` writes `summary`, `topic`, `is_important` and
`auto_flags` onto the Conversation, then raises one `Alert` per firing detector
that has `raises` set, with:

* `alert_type = CONVERSATION` and no `created_by` — nobody created it
* `data["conversation_id"]` — what the detail panel resolves the exchange from
* `data["trigger"]` — one sentence naming the moment, shown above the summary
* `description` — the abstract, editable in the panel like any other overview

`important` on its own does **not** raise; it is a review flag. Alerts come only
from detectors somebody chose in advance, plus the floor. One open alert per
conversation per detector, so a long crisis chat is one row rather than
fourteen.

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

Per-agent model selection, multi-provider (incl. Azure) routing, and the
RAG subtype's document tool are in place. Natural extensions: per-agent
temperature, more tools for prompt-based agents, and OCR so scanned PDFs can be
ingested instead of rejected.
