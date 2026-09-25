# Conversation privacy

A client can ask that one conversation not be readable by their link worker.
Their link worker still sees that it happened, when it ran and how many
messages it held — what is withheld is everything derived from the *words*.

**Off by default.** The switch lives in **Settings → Privacy**
(`SiteConfiguration.conversation_privacy_enabled`, or
`CONVERSATION_PRIVACY_ENABLED` in `.env`) and starts blank. A fresh
installation never sees the feature: no API, no agent tool, nothing different
on any screen.

## What "hidden" means

| Kept | Withheld |
|---|---|
| That the conversation happened | The messages |
| When it started and ended | The classifier's summary |
| How many messages it holds | The topic |
| Which agent held it | The detector answers and the review |
| That an alert was raised from it | The sentence that triggered the alert |
| | The CSV download |

The line is drawn at the words rather than at the bubbles on purpose. An
abstract of a conversation *is* the conversation: withholding the messages
while printing a paragraph describing them would be a promise kept in form
only. The same reasoning removes the rating and the detector checkboxes — a
verdict on an exchange nobody here can read is not a verdict.

The counts stay whole. A navigator seeing "4 messages · 14:02 – 14:08" over a
locked panel knows there is something they have not read, which is the whole
point: the client asked for privacy, not for the conversation to look as
though it never happened.

## Three things it is not

**Not a secret from admins.** An admin holds `access_configuration` and is
answerable for the service; they read everything, here as everywhere else. The
promise made to the client is about their link worker.

**Not stronger than the safety floor.** A conversation that tripped
`SAFETY_DETECTOR` (see `agents.md` → *The self-harm floor*) is readable by the
navigator whatever the client asked. Nothing configurable may stand between a
disclosure and somebody able to act on it, and a privacy switch is
configuration. An agent offering the choice has to say so — the wording in
`native_agents/privacy_tool.py` does. A navigator who reviews the conversation
and answers *no* on that label has said it was not that, and the hide comes
back: `human_flags` wins over `auto_flags`, as everywhere else the flags are
read.

**Not switched off by the switch.** `conversation_privacy.enabled()` gates
whether a client can *make* the request — the API and the agent tool. It is
never consulted when deciding whether an existing hidden conversation is
withheld. An admin turning the feature off stops new requests; it does not
retroactively open exchanges somebody was promised were closed. Settings →
Privacy says how many are still hidden, switch on or off, so the page cannot
quietly imply otherwise.

## Where the rule lives

One function, in `ConvAI/conversation_privacy.py`, because four surfaces read
it and they must not drift:

```python
is_withheld(conversation, user)   # one conversation, one reader
withheld_ids(conversation_ids, user)   # the subset on screen, in one query
```

    hidden AND NOT admin AND NOT self-harm-flagged

| Surface | What it does |
|---|---|
| The detail panel (`views/_panel.py`) | `_conversation_runs` keeps the divider, the span and the count; drops the messages and the download. Chat panes and alert panes both. |
| The client's conversation page (`views/patients.py`) | The card stays in its place in the day, headed *Hidden by the client*. |
| The conversation download (`views/exports.py`) | 404. Checked in the view, not only by dropping the button — the URL is a plain GET anyone can keep. |
| The visibility API (`api/views.py`) | Where the answer is recorded. |

Message bodies are dropped in the **view**, never merely guarded in the
template. A body that does not reach the context cannot be printed by a later
edit to the markup. On the history page that means a `_WithheldThread`
placeholder holds the thread's position in the day, carrying its id and start
time and nothing else.

The full-message export (Settings → Export) is admin-only, so it is unaffected
by construction.

## Classification still runs

`review_conversation` reads the messages straight from the database and is not
privacy-aware, deliberately: the classifier keeps working on hidden
conversations, which is precisely how the things worth raising still get
raised. A hidden conversation can therefore raise an alert, and that alert
names its detector and its priority like any other. What does not travel with
it is the exchange behind it and the sentences the classifier wrote out of it.

## The API

```
GET  /api/v1/conversations/<conversation_id>/visibility/
POST /api/v1/conversations/<conversation_id>/visibility/
Authorization: Bearer <token>        (or "Token <key>" under DRF TokenAuthentication)

{"hidden": true}

-> {"conversation_id": "...", "hidden": true,
    "hidden_at": "2026-09-25T14:03:11Z", "message_count": 4}
```

`message_count` comes back so the agent can tell the client exactly what their
link worker is left with, in the same breath as confirming the change.

Who may call it: the account that holds the conversation (`Conversation.user`),
the tester account standing in for the client on the web chat, or an admin. A
navigator deliberately cannot — the switch is the client's own answer about
their own privacy, and a link worker setting it on their behalf would make it
worth nothing.

Every refusal is a **404**, the feature being switched off included. A caller
who may not touch a conversation should not learn from the status code whether
it exists, and an installation that never turned the feature on has no endpoint
to find. The endpoint is idempotent: an agent whose client says "hide it" twice
should not have to care, and the response always describes the state the
conversation is now in.

`hidden_at` is stamped on the way in and left alone on the way out. What it
answers is "when did they ask for this", and a conversation that was hidden and
then opened again is better described by the fact that it once was than by
having that erased.

## The agent tool

`ConvAI/native_agents/privacy_tool.py` builds two sync LangGraph tools —
`get_conversation_privacy` and `set_conversation_privacy` — over the same
`conversation_privacy` functions the API calls. They take **no conversation
id**: the thread comes from the run config. An id the model could pass is an id
the model could get wrong, and hiding somebody else's exchange because a digit
was hallucinated is not worth an argument that carries no information the
runtime did not already have.

`PRIVACY_PROMPT_SUFFIX` in that module is the wording the agent has to say
before it acts: what the link worker still sees, that an admin can still read
it, that a self-harm disclosure overrides it, and that it covers this
conversation only.

> **Not attached to any agent yet.** The tools exist and are tested;
> nothing hands them to a graph. Wiring them to a prompt-based agent turns that
> agent into a react agent — a plain prompt agent has no tool loop at all — and
> the conversation that *asks* the client the question is the next piece of
> work. Until then the switch is reachable through the API and the Django admin
> (Conversation → Privacy).

## Data model

| Field | What it holds |
|---|---|
| `Conversation.hidden` | The client's answer. Indexed, defaults False. |
| `Conversation.hidden_at` | When they last asked for it. |
| `SiteConfiguration.conversation_privacy_enabled` | The installation switch, tri-state like every other. |

Per conversation and nothing wider. The client is answering "this one", not
signing a standing policy — and a thread rolls over after a couple of hours
idle, so the next conversation starts visible and they are asked again if the
agent offers it again. A client-level default would be a different feature, and
a more dangerous one: a standing "hide everything" is a client whose link worker
stops seeing their care.

Migration `0087_conversation_privacy`, additive and off by default.

Tests: `ConvAI/test_conversation_privacy.py`.
