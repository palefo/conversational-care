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

A JSONField rather than a field pair per tool, for the same reason `detectors`
is one: a third tool should not need a migration.

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

Remote agents run off-box and cannot reach the ORM, so they use REST. Every
remote run now carries the identity to do it:

```python
configurable = {
    "thread_id": ..., "user_id": ..., "user_name": ...,
    "conversation_id": ...,   # the same id as thread_id, under the obvious name
    "patient_id": ...,
    "client_id": ...,         # alias: the data model says Patient, the UI says client
    "user_token": ...,        # a Conversational Care API token
}
```

Sent on every run, not only when a tool is enabled: for a remote agent the
enabled-tool list lives on the remote side, so a gate here would be guessing,
and an id a graph ignores costs nothing.

```
GET  /api/v1/conversations/<conversation_id>/summary/
POST /api/v1/conversations/<conversation_id>/summary/
Authorization: Bearer <token>

{"summary": "..."}

-> {"conversation_id": "...", "summary": "...", "source": "agent",
    "agent_summary": "...", "agent_summary_at": "...", "hidden": false}
```

`source` comes back so an agent can tell whether its own report is the one on
screen. Idempotent: each report replaces the last, and the response describes the
state the conversation is now in. A blank summary is a 400 rather than a quiet
delete — erasing a summary a navigator may already have read is a different act
from writing one, and nothing has asked for it. Over 2000 characters is a 400
too; this is a summary, not a second transcript.

**Not behind a feature switch**, unlike the sibling visibility endpoint. Gating
that one is load-bearing — it decides whether the platform may make a promise to
a client. A summary is the same kind of thing the classifier already writes
unasked on every conversation, so a switch would only mean a fresh
installation's agents fail silently.

The Python SDK has `report_conversation_summary`, `get_conversation_summary`,
`set_conversation_visibility` and `get_conversation_visibility`.

## Who may act on a conversation

`ConvAI/conversation_actors.py`, shared by both endpoints:

    admin  OR  conv.user  OR  the patient's tester account

Bound to the **patient**, not to a user account, because on the channel most
clients actually use there is no account to bind to. `save_message` is the single
path every WhatsApp, SMS, external-chat and tester turn is persisted through, and
it has never set `Conversation.user` — only `/api/v1/messages/` does, for SDK
callers. A check that could only match `conv.user` worked for the SDK and the web
tester chat and silently 404'd for everybody on WhatsApp. The visibility endpoint
had this hole from the day it shipped.

A **navigator is deliberately not on the list.** For visibility that is
load-bearing: the switch is the client's own answer about their own privacy, and
a link worker setting it on their behalf would make it worth nothing. For the
summary it is consistency — a navigator has their own summary block in the panel,
which is a better place for their words than the agent's field.

### The service account, and what it costs

`token_for_conversation` hands the agent the narrowest credential that exists:
the conversation's own account, then the patient's tester account, then a
login-disabled **`agent-service`** account created on demand. That last one holds
`access_configuration`, so it matches the `admin` branch above.

Which means, plainly: **a service token can write to any conversation.** What
keeps it to the right one is the tool, which takes its conversation id from the
run config and never from a model-supplied argument — not the permission check.
The trade accepted here is that the in-process tool is code we control, while the
alternative (a capability token minted per run, naming one conversation) is a
second authentication path to get wrong. What this design does instead is make
the writes traceable: every summary and visibility write is logged with the
acting account's name, and the service account is never a person, so the log does
not read as an admin who was asleep at the time.

If that trade stops being acceptable — a remote agent on infrastructure we do not
control, say — the replacement is the per-run capability token, and
`conversation_actors` is the one module it would touch.

## Data model

| Field | What it holds |
|---|---|
| `Conversation.agent_summary` | The agent's own summary. Preferred over `summary`. |
| `Conversation.agent_summary_at` | When it last reported one. |
| `SummaryEdit.body` | A person's summary, beside the machine's. Blank on the in-place kinds. |
| `Agent.tools` | Enabled tool slugs, with optional prompt overrides. |

Migration `0088_agent_summary_and_tools`, additive plus the backfill above.

Tests: `ConvAI/test_conversation_summary.py`.
