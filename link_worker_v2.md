# Link Worker v2 (beta) and client records

Staff can now ask the chat bubble about their clients, not only find them and
book meetings:

* *When is my next meeting?* — and with whom, for the whole caseload.
* *What did Margaret report in her wellbeing calls?*
* *How have her IQCODE answers changed?* — the evolution of one protocol, call by call.
* *Tell me everything we have on Thomas.*
* *Which of my clients mentioned falls?*

A navigator asks about their own clients; an admin about everyone. The same
questions are available over the REST API and in the downloadable SDK.

**Off by default.** Settings → Agents → *Chat bubble assistant* switches the
bubble from the original Link Worker to v2. With it off the bubble is exactly
what it was — v1, the same header, the same hint — v1 itself is untouched, and
the new client-record endpoints answer 404 as if they did not exist: an
installation that has not opted in gains no new way to read every record.

## The shape: one service, two doors

```
            chat bubble                         REST API / SDK
                 │                                    │
      Link Worker v2 (native agent)          client-record endpoints
       7 tools, sync, in-process              personal-token auth
                 │                                    │
                 └──────────► ConvAI/client_records.py ◄───┘
                              who may see whom · what is withheld
                              the answer's shape · the access log
```

`ConvAI/client_records.py` is the only code that reads a client's record for
either. The tools hand the model the **same JSON** the endpoints return, so the
assistant and the API cannot disagree about who may read what, or describe one
record two ways.

| Question | Service function | Tool | Endpoint | SDK |
|---|---|---|---|---|
| Find a client | `find_clients` | `find_clients` | — (the existing `GET /api/v1/patients/?q=`) | `list_patients` |
| Everything we hold | `overview` | `client_overview` | `GET /api/v1/patients/<id>/overview/` | `client_overview` |
| Next meetings | `upcoming_meetings` | `upcoming_meetings` | `GET /api/v1/meetings/upcoming/?patient_id=&days=` | `upcoming_meetings` |
| What they reported | `protocol_answers` | `protocol_answers` | `GET /api/v1/patients/<id>/protocols/answers/?protocol=` | `protocol_answers` |
| How a protocol changed | `protocol_history` | `protocol_history` | `GET /api/v1/patients/<id>/protocols/<protocol>/history/` | `protocol_history` |
| Across the caseload | `search_records` | `search_records` | `GET /api/v1/records/search/?q=` | `search_records` |
| Book a meeting | v1's `_schedule_meeting` | `schedule_meeting` | the existing `POST /api/v1/meetings/` | `schedule_meeting` |

A protocol is named by its number or by words from its title — `3`, `"3"` and
`"iqcode"` all find protocol 3. Words that match several titles are refused with
the candidates listed, rather than answered about whichever sorted first.

## Decisions

**1. A new agent beside v1, not an upgrade of it.** v2 is its own native agent
(`native_key = "link_worker_v2"`, seeded by migration `0092`). Every
installation keeps v1 until an admin chooses otherwise, and a beta that
misbehaves is one switch away from gone.

**2. It reads, and books meetings — nothing else.** All the new tools are
read-only. Scheduling is v1's own function, so it keeps v1's permission check
and its one-hour clash rule. No notes, no edits, no messages sent.

**3. The screens' rule, exactly.** `visible_patients(user)`: an admin (the
`access_configuration` permission, as everywhere in the app) sees every client;
anybody else sees the clients they are the navigator of. That is the rule the
Clients page and a client's page already apply. A client outside it is refused
exactly as a client that does not exist (`NotVisible`, a 404 over the API), so
the answer never confirms that somebody is on the platform.

**4. Summaries, never transcripts.** No tool reads a message body. Conversations
arrive as their machine summary. One the client hid from their link worker
gives its summary and the fact that it was hidden — no topic, no link worker's
summary — which is what the client's own page shows (see
[conversation_privacy.md](conversation_privacy.md)). An alert raised from such a
conversation keeps its title and loses what the classifier wrote out of the
exchange, as on the alert panel. Search reads only the summary a screen would
show — the agent's where it wrote one — never a summary it superseded. An admin,
as on screen, is not withheld from.

**5. Every look is logged.** Each function that returns a client's record writes
a `RecordAccess` row per client it revealed: who, which client, when, which
question, and whether through the assistant or the API. A search logs every
client it matched. Finding a client by name logs the query once — a name list
is not a record. The log is append-only (the model refuses a re-save) and
look-only in the Django admin; labels are copied, so a row still reads after the
account or the client is deleted.

**6. Who is asking comes from the server, never a token.** v1 puts the
navigator's *personal API token* into the run config. LangGraph copies every
string value of the run config into run metadata, which is what tracing reads
— so v2 gets none. It is told `staff_user_id`, which only staff surfaces set —
the chat bubble, and the admin-only Test chat on the Agents page — and it
ignores `user_id`: in a client's own conversation that is the *client's*
id, and a client whose id happened to match a navigator's would otherwise be
answered as that navigator. And v2 is never offered for a client in the first
place: every picker that gives a client (or a client-facing flow) an agent
lists `Agent.for_clients()`, which leaves out staff-only agents, and the views
that save the choice check it too. Were it assigned anyway, its tools would say
it only answers staff.

**7. Structured questions, not retrieval over records.** The tools run ordinary
queries; nothing about a client is embedded or indexed. Answers are as current
as the database, and there is no index of anybody's health information to
build, refresh or secure. (What the assistant *said* is kept with the
conversation, as for any agent — see Limitations.)

**8. The model reads what was said; the platform does not code conditions.**
Protocol answers are free text. "What conditions did she report?" is answered by
the model reading her answers and details, and the prompt tells it to report
what was said and not to turn a symptom into a diagnosis. Coding answers into
conditions (SNOMED and the like) would be a separate, much larger decision.

**9. The latest answer wins; the history keeps them all.** `protocol_answers`
applies the call panel's rule — the most recent answer to a question replaces
the one it corrects. `protocol_history` returns every call's answers oldest
first, each beside that meeting's own protocol summary, which is what "has it
improved?" needs.

**10. Bounded answers.** Lists stop at 20–30 rows and say when they were cut;
a tool answer over 12,000 characters is cut with a note telling the model to ask
a narrower question. A caseload-wide question should not be able to crowd the
conversation out of the model's context.

**11. Low temperature.** v2 runs at 0.2 (v1 at 1.0). It reports what a record
says, and a model that paraphrases creatively misquotes a client.

## On WhatsApp

Navigators can also reach v2 from their own phone — one client at a time, which
they load. See [link_worker_whatsapp.md](link_worker_whatsapp.md).

## Configuration

| Setting | Default | What it does |
|---|---|---|
| `LINK_WORKER_V2_ENABLED` | off | Which assistant the chat bubble runs, and whether the client-record endpoints exist. Also in **Settings → Agents**. |

The model is the v2 agent's own `Agent.model` (Agents page), else
`DEFAULT_AGENT_MODEL`, like every in-process agent.

## Limitations

* **Beta.** The tools are tested; how well a given model uses them is not yet
  measured on real questions from link workers.
* **Notes on call recordings are not read.** A recording finds its client by
  phone number rather than by a link; the overview and search read notes on
  meetings, alerts and conversations.
* **The log grows without bound.** There is no retention rule yet, and no
  screen in the app — it is in the Django admin under *Record accesses*.
* **The older endpoints disagree about who is an admin.** `GET
  /api/v1/patients/<id>/meetings/` and `…/protocols/filled/` treat `is_staff`
  as admin, where the app (and these new endpoints) use the Configuration
  permission. Left as it was, deliberately; worth aligning separately.
* **A conversation with the assistant keeps what it was told.** Like every
  agent, v2's conversation state — including what its tools returned — is kept
  in the LangGraph checkpoint store, and its replies are saved in the
  navigator's chat history. Those copies follow the conversation, not the
  client's record, and need a retention rule of their own before wider use.
* **Record text is not trusted as instructions**, and the prompt says so; but
  some of it began as what a client typed. What v2 can be talked into is
  bounded by what it can do: read what the navigator could already see, and
  book a meeting.
* **Remote staff agents cannot use this yet.** The endpoints act as the person
  holding a personal token. Letting a remote LangGraph agent act *for* a
  signed-in navigator needs a delegated token — a design of its own.
* **v1's clock can disagree with the platform's.** v1 tells its model the time
  in `DEFAULT_TIMEZONE` (UTC when unset); the platform writes times in
  `TIME_ZONE` (`PLATFORM_TZ`, Europe/London by default). v2 uses the platform's.
  Pre-existing in v1 and left alone.
* **v1's Test chat on the Agents page cannot use v1's tools.** It never passes
  v1 the token they need. Pre-existing and left alone; v2's Test chat works.
* **Search is by phrase, not meaning.** It matches at the start of a word, so
  "fall" finds "falls" and "falling" — "falling behind on a bill" included, which
  the model sees in the snippet and can discount — but never "slipped on the
  stairs".

## Tests

`ConvAI/test_client_records.py` — the permission rule, the privacy rule, the
access log, each endpoint, the SDK's paths (routed through Django's test
client), the agent's real graph and tools driven by a scripted model, the
bubble's routing either way, and the bubble's markup with the switch off.

```bash
python3 manage.py test ConvAI.test_client_records --settings=test_settings
```
