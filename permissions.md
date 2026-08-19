# Users, Roles & Permissions

Conversational Care has a small, explicit role model built on **Django groups and
permissions**. This document describes the user types, what each can do, and the
helpers developers use to enforce access.

## Concepts

- **Clients are not users.** A client (the person receiving care, plus their
  caregiver) is stored as `Patient` / `Caregiver` records and monitored through the
  chatbot. They never get a login account.
- **Roles are derived**, not stored as a single field:
  - *Admin* = Django **superuser** (holds the `access_configuration` permission).
  - *Navigator* = member of the **`Navigator`** group.
  - *Test user* = member of the **`PatientTester`** group, linked 1:1 to one `Patient`.
- The `Navigator` and `PatientTester` groups are created automatically by a data
  migration; the `access_configuration` permission is defined on `SiteConfiguration`.

## User types

| Role | How it's identified | Sees | Configuration | Notes |
|------|---------------------|------|---------------|-------|
| **Admin** | `is_superuser` (has `access_configuration`) | Everything — all clients, calls, alerts | ✅ Full access | Created via `createsuperuser` |
| **Navigator (CTN)** | `Navigator` group, non-staff | **Only their own clients** and related calls/alerts | ❌ No access | Created from the Clients page |
| **Test user** | `PatientTester` group, non-staff, linked to a client | Nothing but the **audio chatbot** for their linked client | ❌ No access | Created from the Clients page |
| **Client** | Not a user | — | — | `Patient`/`Caregiver` record only |

### What each role can do

- **Admin** — the full app plus the **Settings** page (integrations, branding,
  messaging, protocols, maintenance) and user management. Admins bypass the
  "own-clients-only" filter and see every client.
- **Navigator** — dashboard, their clients, calendar, calls and alerts,
  **scoped to clients where they are the assigned navigator**. They cannot open
  Settings or create users.
- **Test user** — on login they are sent straight to the **audio chatbot** for the
  client they are linked to, and are blocked from every other page. Used to
  exercise a specific client's conversational experience.

## Creating users

- **Admins**: `docker compose exec web python manage.py createsuperuser`.
- **Navigators & Test users**: from the admin-only **Users** page (left menu →
  *Users*), click **"Add user"** and choose the role:
  - *Navigator* — a plain login account added to the `Navigator` group.
  - *Test user* — linked to a chosen client; only one test user is allowed per
    client, and it can only reach the audio chatbot.

  The Users page (and this UI) is visible and usable **only to admins**.

## Agents

The admin-only **Agents** page (left menu → *Agents*) lists the LangGraph agents used
for chat and classification, and lets admins create, edit, and delete them — an
in-app equivalent of the Django admin for the `Agent` model.

## For developers

Role logic lives in [`Django_CMS/ConvAI/roles.py`](Django_CMS/ConvAI/roles.py):

```python
from ConvAI.roles import (
    is_admin, is_navigator, is_tester,          # boolean helpers
    admin_required, navigator_required, tester_required,  # view decorators
)
```

- **Predicates** — use in views/templates to branch behaviour (e.g. show all
  clients vs only the current navigator's):

  ```python
  qs = Patient.objects.all()
  if not is_admin(request.user):
      qs = qs.filter(navigator=request.user)
  ```

- **Decorators** — gate whole views:

  ```python
  @admin_required          # superuser / access_configuration — Settings & user mgmt
  @navigator_required      # admin or Navigator group — main app pages
  @tester_required         # admin or PatientTester group — external audio chat
  ```

Inside the view package these helpers are re-exported through
`ConvAI/views/_base.py`, so any view module gets them via `from ._base import *`.

### Notes & gotchas

- **Admin = superuser.** Only superusers are admins. Any legacy `is_staff=True`
  account that is *not* a superuser behaves as a navigator (own clients, no
  configuration). Audit your user table if you relied on plain staff accounts.
- **`access_configuration` permission.** `admin_required` checks this permission
  (not `is_superuser` directly), so a future non-superuser "admin" group can be
  granted configuration access without code changes.
- Created accounts get an inline password — there is no invite/reset email flow yet.
