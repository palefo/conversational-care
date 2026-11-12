# Field App integration

[Field App](https://github.com/palefo/field_app) is the tablet app link workers
use on home visits to fill in questionnaires. This integration lets an agent use
what was collected there: each turn, the agent is given **the Field App record
of the client the conversation is with**, and no one else's.

It lives in its own app, `field_app/`, and reaches the core only through
`ConvAI/extensions.py` (the add-on registry). Leave it out of a deployment
with `FIELD_APP_INTEGRATION=0`. Until an admin registers a Field App
installation, it adds nothing to any page.

## Setting it up

1. **In Field App** (as staff): Settings → Third-party apps → *Register an
   app*. Use Conversational Care's callback address, which is shown on CC's
   Settings → Third-party apps, e.g. `https://care.example.org/field-app/callback/`.
   Field App then shows a client ID and a client secret. The secret is shown
   once.
2. **In Conversational Care** (as admin): Settings → **Third-party apps** →
   *Add a Field App installation*:
   - **Field App address**: where people open Field App in the browser.
   - **Server address** (optional): how CC's server reaches Field App, when that
     differs. When both run in Docker on one machine, the browser uses
     `http://localhost:8003` but CC's container needs
     `http://host.docker.internal:8003`. Field App must then also accept that
     host (`DJANGO_ALLOWED_HOSTS`).
   - **Client ID** and **Client secret** from step 1.

## Using it

| Who | Where | What |
| --- | --- | --- |
| Link worker | **Profile → Field App → Connect Field App** | Opens Field App's consent screen. After *Allow*, CC stores a **read-only** token that acts as that Field App user. *Disconnect* revokes it in Field App too. |
| Link worker | **Client page → Field App** | Search Field App (name or phone, optional date of birth) with your own connection and **Link** the right athlete. **What agents see** previews the record. **Unlink** removes the link. |
| Admin | **Agents → (prompt agent) → Data sources** tab, next to the tools | Tick *Give the agent the client's Field App record*. Choose how many rounds per questionnaire, and optionally replace the default instructions. |

## What the agent gets

For a prompt agent with the data source on, `run_prompt_agent` asks the
add-ons for context (`extensions.agent_context(agent, thread_id)`). The Field
App add-on then:

1. finds the conversation's client from the run's `thread_id`. The model never
   chooses whose record is read;
2. finds that client's link to a Field App athlete. If there is none, nothing
   is added;
3. reads the record with the connection of the **client's link worker**
   (navigator), falling back to whoever made the link. A connection unrelated
   to the client is never used;
4. fetches `GET /api/v1/patients/<athlete>/context/?as=markdown` from Field App,
   caches it for 60 seconds, and appends it to the system prompt:

```
## Field App record

Below is this client's record from Field App, … If the client contradicts it, believe the client.

<field_app_record source="Field App">
# Field App record: Ada Demo
## Home visit check-in
### Collected 2026-11-09 17:03 by admin
- How are you feeling today? — **Okay**
…
</field_app_record>
```

It is fetched on every turn, so new answers from a home visit reach the agent
within a minute. If Field App can't be reached, nothing is added and the turn
goes on. If Field App rejects the token (revoked there, or the app switched
off), the connection is marked broken and the link worker is asked to connect
again.

Only **prompt agents** have the tab, because only they have a system prompt
CC assembles. Remote (LangGraph) agents could receive the same Markdown through
the run config later.

## Data model (`field_app/models.py`)

| Model | Holds |
| --- | --- |
| `FieldAppInstallation` | Field App address, optional server address, client ID and secret, on/off. |
| `FieldAppConnection` | One per (installation, user): the read-only token, and the Field App username it acts as. |
| `PatientFieldAppLink` | One per (client, installation): the Field App athlete id and name, who linked it, and when. |
| `AgentDataSource` | One per (agent, installation): on/off, rounds and instructions. |

Like the platform's other integration credentials (Settings → Integrations),
the client secret and tokens are stored as plain database columns. Protect the
database and its backups accordingly.

## Running both locally

Conversational Care in Docker on `:8002` and Field App on `:8003`. Both are
Django apps on `localhost`, and browsers don't separate cookies by port, so
Field App uses its own cookie names (`fieldapp_sessionid`,
`fieldapp_csrftoken`). Otherwise signing in to one would sign you out of the
other mid-flow.

## Tests

```bash
docker compose exec web python manage.py test field_app --settings=test_settings
```

Field App is never called in the tests; its HTTP is mocked.
