# Agent tools, and the summary an agent writes

Two things that arrived together, because the second is the first one's reason
to exist: agents can now be given **platform tools** on the agent form, and one
of those tools lets an agent **write the summary its client's link worker
reads**.

## The summary a navigator reads

A conversation can carry three summaries, from three different authors:

| Where | Written by | Editable by a navigator |
|---|---|---|
| `Conversation.agent_summary` | The agent that held the conversation | No |
| `Conversation.summary` | The classifier, reading the transcript afterwards | No |
| `SummaryEdit.body` | The navigator | Yes — it is theirs |

**One machine summary is shown, and the agent's wins.** The agent was in the
conversation; the classifier read a transcript of it after it ended. Showing
both would print two paragraphs that usually say the same thing twice, so the
panel picks one and says in the byline which — *Reported by Companion* or
*Generated automatically*.

**The machine summary is read-only, and the navigator's sits beside it.** It
used to be editable in place, which meant the model's original and a
navigator's correction could not both exist. Worse, once an agent could report
its own summary at any point in a conversation, a navigator's rewrite would be
shadowed by the next report with no explanation. Two blocks, neither overwriting
the other, answers both: the record keeps what the model claimed *and* what the
person who knows better says.

**Per conversation, not per day.** A chat panel is one day of a client, and a
day is often several conversations. Each gets its own entry on the Summary tab,
and its summary also sits on its own divider on the Conversation tab — in place
of the messages on a hidden one, which is exactly where a link worker looks. It
used to be one summary for the day, the last conversation's, so a hidden
conversation earlier in the day showed no summary anywhere.

**No link-worker summary on a hidden conversation.** You cannot summarise what
you cannot read, so the block is not offered, and one written before the client
hid the conversation is withheld with the words it was written from. Follow-up
belongs in Notes, which is about what the link worker did rather than what was
said. The machine summary is still shown on a hidden conversation — the
agent's, or the classifier's when the agent reported none; see
conversation_privacy.md.

The link worker's block is headed *Your summary* only when it is; a reassigned
link worker reading a predecessor's words sees *Summary by …*.

Meetings, recordings and alerts keep their in-place edit. Nothing about them
changed — a navigator who was on the call really does know better than the
model, and that overview has only one author to disagree with.

The rule lives in `ConvAI/conversation_summary.py`, in one place, because the
detail panel, the client's history page and the REST endpoint all read it.

### Migration 0088 moves text

A navigator who had already corrected a conversation summary overwrote
`Conversation.summary` in place. That text is a person's words sitting in the
machine's field, and would now print under a *Generated automatically* byline —
exactly the false claim `SummaryEdit` exists to prevent. The migration therefore
**moves** it to `SummaryEdit.body` and clears `summary`, rather than copying it.
Copying would print the same paragraph twice under two bylines; leaving it would
keep crediting a person's writing to a model. Clearing loses nothing that still
existed — the model's original was gone the day they edited it. Reversible.

## Tools on a prompt-based agent

The agent form has a **Tools** block of checkboxes, and a tab strip: the agent's
own system prompt, a tab per enabled tool, and a **Preview** of the two
concatenated. What ships:

| Slug | Checkbox | What it gives the agent |
|---|---|---|
| `conversation_privacy` | Conversation privacy | `get_conversation_privacy`, `set_conversation_privacy` |
| `report_summary` | Report summary | `report_summary` |

`ConvAI/native_agents/tool_registry.py` is the only place that knows which tools
exist. A third one is a dict entry and a builder — no form, template, migration
or graph change.

### Storage, and what "Reset to default" means

```python
Agent.tools = {"conversation_privacy": {}, "report_summary": {"prompt": "..."}}
```

Presence of the key means **enabled**. An *absent* `prompt` key means **use the
shipped default**, and that is the load-bearing part: had the default been
copied into the row on save, improving the shipped wording would reach no
existing agent, and "Reset to default" would write a copy that is right only
until the next release. Reset is a key deletion. The form compares the submitted
text against the shipped default on every save and stores `{}` when they match,
so resetting and saving does the same thing whether the admin used the button or
retyped the text.

Unticking a tool whose wording was edited keeps the words and switches it off —
`{"enabled": false, "prompt": "…"}` — so an accidental untick and save does not
throw anybody's work away. Unticking an unedited tool removes its entry.

A JSONField rather than a field pair per tool, for the same reason `detectors`
is one: a third tool should not need a migration.

The Preview tab leaves out a ticked tool the installation has switched off,
exactly as the server does, and names it underneath instead.

### Assembly

The system prompt is `system_prompt`, then each enabled tool's wording, in
**registry order** — not in the order the boxes were ticked, and not in the order
the keys are stored. That last one is not a precaution: `Agent.tools` is a
`jsonb` column, and Postgres does not keep the key order it was given, so a row
saved with privacy first reads back with `report_summary` first. Reading the
stored order would silently reorder the assembled prompt between saves. The
Preview tab shows exactly what will be sent, so an admin reads it rather than
imagining it.

A tool contributes its wording **only if it also contributes its tool**. An
agent with Conversation privacy ticked on an installation where the feature is
switched off gets neither. A prompt describing a tool the graph was not given is
how you get an agent telling a client their conversation is hidden when nothing
hid it.

### Any tool means a react agent

A plain prompt agent is a single model-call node with no tool loop, so a tool
handed to it would never be called. `build_prompt_graph` takes the react branch
whenever there is at least one tool, RAG's `search_documents` included. The form
does not ask, because "you must also tick this other box" is a question with one
correct answer.

**Tools and real-time voice are refused together**, matching the existing rule
for RAG and for the same reason: a real-time agent converses straight with the
voice API and never calls a tool.

## How a remote agent calls back

Remote agents run on their own LangGraph server and cannot reach the database,
so reporting a summary or hiding a conversation means calling the REST API.

### What they are sent

```python
configurable = {
    "thread_id": …, "user_id": …, "user_name": …,
    "conversation_id": …,   # the same id as thread_id, under the obvious name
    "patient_id": …,        # informational
    "client_id": …,         # alias: the model says Patient, the UI says client
    # Only when the Agent has "Allow callbacks" on:
    "cc_run_token": …,      # scoped to this conversation, 2 hours
    "cc_api_url": …,        # AGENT_CALLBACK_URL, if the installation sets it
}
```

The ids are sent on every run and are **informational**: nothing on the platform
trusts them. What an agent may act on is decided by the token.

**Personal API tokens are never sent to an agent.** An earlier version handed a
remote agent the conversation owner's token, an admin's own token when an admin
was testing the agent, or an admin-privileged `agent-service` account's. Each of
those is a whole person's access — every endpoint, every client that person can
see, no expiry — in the run config of a server that stores it. That account and
that code are gone.

### The run token

`ConvAI/run_tokens.py`. Signed with Django's `signing` under its own salt, and
stored nowhere: verifying one is a signature check and a clock comparison.

| | |
|---|---|
| **Names** | one conversation, its client (or none), the agent it was issued to |
| **Allows** | `summary`, `visibility` — nothing else |
| **Accepted by** | `/api/v1/run/` and nothing else, because no other endpoint lists its authentication class |
| **Expires** | after 2 hours, the conversation's own idle window |
| **Issued** | only to a remote Agent with *Allow callbacks* on; in-process agents get none |

The conversation is created before the run when callbacks are on, so a client
whose *first* message is "don't let my link worker see this" can be answered on
that turn.

### The endpoints

```
GET  /api/v1/run/                {} -> conversation_id, patient_id, summary, source,
                                       hidden, privacy_available, scopes, expires_at
POST /api/v1/run/summary/        {"summary": "…"}
POST /api/v1/run/visibility/     {"hidden": true}
Authorization: RunToken <cc_run_token>
```

**No conversation id anywhere.** The token says which, so there is nothing for
the agent — or a model inside it — to forge or get wrong, and a token issued for
one conversation cannot reach another. Every refusal of a valid token is a 404,
as elsewhere in this API. A personal token is refused here; a run token is
refused everywhere else.

### In an agent: three lines

The client SDK ships the tools, self-contained so an agent server can vendor two
files (`run_client.py`, `langgraph_tools.py`) without the rest of the SDK:

```python
from conversationalcare_api.langgraph_tools import build_cc_tools, PRIVACY_PROMPT

graph = create_react_agent(model=model,
                           tools=[get_current_date, *build_cc_tools()],
                           prompt=… BASE_PROMPT + PRIVACY_PROMPT …)
```

The tools take no ids, read `cc_run_token` from the run config, and tell the model
"not connected" instead of raising when there is none (`langgraph dev`, tests,
callbacks off). Their prompt wording is the platform's own; the test suite fails
if the SDK's copy drifts from it (`ConvAI/test_sdk_run_tools.py`). An agent that
nests graphs must pass `config` to the sub-graph explicitly, or the token does not
reach it.

RECO v2 (`reco_v2/` in the agent collection) is wired this way, with
**Conversations start hidden** on: it asks the young person whether their link
worker may read the conversation and unhides it only on a yes to that question
— checked in code, not just the prompt — and reports a two-line note
(`report_summary`) whatever they answer. An agent whose conversations start
hidden uses `PRIVACY_PROMPT_STARTS_HIDDEN` in place of `PRIVACY_PROMPT`.

### A turn of several messages

A graph may answer one message with several AI messages — RECO v2 swaps its
`[SUMMARY]` token for the summary's texts and then asks whether it looks right,
in one turn. The platform used to forward only the last one.
`utils.remote_turn_reply` now joins, with blank lines, every AI text after the
person's message and after the turn's last tool step: the tool-step boundary
keeps a supervisor-style graph to its final answer, whose drafts and hand-backs
come before a tool message.

## Who may act on a conversation (people)

`/api/v1/conversations/<id>/summary/` and `…/visibility/` remain, for **people**
with their own API token. `ConvAI/conversation_actors.py`:

    admin  OR  conv.user  OR  the patient's tester account

A **navigator is deliberately not on the list.** For visibility that is
load-bearing: the switch is the client's own answer about their own privacy, and
a link worker setting it on their behalf would make it worth nothing.

## Data model

| Field | What it holds |
|---|---|
| `Conversation.agent_summary` | The agent's own summary. Preferred over `summary`. |
| `Conversation.agent_summary_at` | When it last reported one. |
| `SummaryEdit.body` | A person's summary, beside the machine's. Blank on the in-place kinds. |
| `Agent.tools` | Tool slugs, with optional prompt overrides and `enabled: false` for an unticked tool whose wording was edited. |
| `Agent.allow_callbacks` | Remote agents: issue a run token on each run. Off by default. |

Migration `0088_agent_summary_and_tools`, additive plus the backfill above;
`allow_callbacks` in `0089`.

Tests: `ConvAI/test_conversation_summary.py`, `ConvAI/test_run_tokens.py`,
`ConvAI/test_sdk_run_tools.py`.
