# Database setup

Conversational Care stores everything in **PostgreSQL**. The connection is
entirely driven by the `DB_*` variables in `Django_CMS/.env`, so you can either
let Docker run a database for you or point the app at one you already have.

All commands below are run from the `Django_CMS/` directory (where
`docker-compose.yml` and `.env` live):

```bash
cd Django_CMS/
cp .env.sample .env      # first time only
```

The relevant variables (see `.env.sample`):

| Variable | Meaning |
| --- | --- |
| `DB_HOST` | Where the app finds Postgres (`db` = the bundled container; a hostname/IP for an external one). |
| `DB_PORT` | Port the app connects to (5432 inside the Docker network for the bundled DB). |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | Database name and credentials. |
| `DB_SSLMODE` | `disable` for a plain local DB; `require` for managed DBs (e.g. Supabase, RDS). |
| `POSTGRES_PUBLISH_PORT` | *(bundled DB only)* Host port that exposes the container's Postgres. Does **not** change `DB_PORT`. |

---

## Option 1 — Run everything in Docker (default, recommended)

The bundled `docker-compose.yml` starts a Postgres container (`db`) alongside the
app (`web`). The database name and credentials come from the same `DB_*`
variables, so it works with the defaults out of the box.

```bash
docker compose up -d                              # starts db + web
docker compose exec web python manage.py migrate  # first-time schema setup
docker compose exec web python manage.py createsuperuser
```

The app is now at <http://localhost:8000>. Data persists in the `postgres_data`
Docker volume across restarts and rebuilds. Nothing else to configure.

---

## Option 2 — The default Postgres port is already in use

**Symptom:** `docker compose up` fails with something like
`Bind for 0.0.0.0:5432 failed: port is already allocated` — usually because the
server already runs another Postgres on port **5432**.

The app talks to the bundled database over Docker's internal network, so this is
purely about the **host** port that Postgres is *published* on. Pick a free host
port with `POSTGRES_PUBLISH_PORT` in `.env`:

```dotenv
# .env
POSTGRES_PUBLISH_PORT=5433
```

Then bring it up as usual:

```bash
docker compose up -d
```

- The app is **unaffected** — `DB_HOST=db` / `DB_PORT=5432` still connect over
  the internal network. Leave `DB_PORT` at `5432`.
- To reach the bundled DB from the host (e.g. with `psql`), use the new port:
  ```bash
  psql -h localhost -p 5433 -U djangocms_user djangocms_db
  ```
- If you don't need host access to the database at all, you can instead delete
  the `ports:` block under the `db` service in `docker-compose.yml` — the app
  keeps working over the internal network.

---

## Option 3 — Use an existing local or remote Postgres

Point the app at your own database (another Postgres on the server, a managed
service like Supabase/RDS, etc.) and **don't** start the bundled `db`.

**1. Set the connection in `.env`:**

```dotenv
DB_ENGINE=django.db.backends.postgresql
DB_HOST=<your-postgres-host>
DB_PORT=5432
DB_NAME=<db-name>
DB_USER=<db-user>
DB_PASSWORD=<db-password>
DB_SSLMODE=disable          # use 'require' for managed/remote DBs that enforce TLS
```

Getting `DB_HOST` right from inside the container is the common pitfall:

| Where your Postgres runs | `DB_HOST` |
| --- | --- |
| On the **host machine** | `host.docker.internal` (Docker Desktop / recent Docker), or the host's LAN IP. **Not** `localhost` — inside a container that means the container itself. |
| In **another container** | Put both on the same Docker network and use that container's name. |
| **Remote / managed** (Supabase, RDS, …) | The provider's hostname, with `DB_SSLMODE=require`. |

**2. Make sure the database and role exist.** The bundled `db` auto-creates them;
an external Postgres will not. Create the database and user (with privileges to
it) beforehand.

**3. Start only the app (skip the bundled database):**

```bash
docker compose up -d --no-deps web
```

`--no-deps` starts `web` without bringing up the `db` service.

**4. Run migrations and create an admin user:**

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

> **Note:** the in-process ("native") agents use a LangGraph checkpointer that
> reuses these same `DB_*` credentials — no separate database configuration is
> needed.

---

## Verifying the connection

```bash
docker compose exec web python manage.py migrate   # succeeds → the DB is reachable
docker compose exec web python manage.py dbshell    # opens a psql session
```

If a connection fails, check in this order: `DB_HOST` reachability from inside the
container, `DB_SSLMODE` (a plain DB needs `disable`; a managed one needs
`require`), and that `DB_NAME`/`DB_USER`/`DB_PASSWORD` match a database that
actually exists.

---

## **Database Backup and Restore**

This section provides instructions for creating and restoring a complete backup of your PostgreSQL database. The backup is exported as a `.tar` archive, which bundles the database schema (SQL creation instructions) and the table data into a single file.

### Create a Backup

Ensure your Docker containers are actively running before attempting to create a backup.

1.  **Open a terminal** and navigate to the `Conversational-Care/Django_CMS/` directory (where your `docker-compose.yml` is located).

2.  **Run the backup command:**
    This command safely executes the `pg_dump` utility inside the running `db` container and extracts the data to a file named `djangocms_backup.tar` on your local machine.
    ```bash
    docker compose exec -T db pg_dump -U djangocms_user -F t djangocms_db > djangocms_backup.tar
    ```
    *(Note: The `-T` flag is important as it prevents terminal formatting from corrupting the backup file).*

### Restore a Backup

If you ever need to load your data back into the database from a previous `.tar` backup, use the native `pg_restore` command.

1.  **Open a terminal** in the directory where your `djangocms_backup.tar` file is located.

2.  **Run the restore command:**
    ```bash
    docker compose exec -T db pg_restore -U djangocms_user -d djangocms_db < djangocms_backup.tar
    ```
    *(Note: If prompted for a password during backup or restore, use the password defined in your docker-compose file: `djangocms_pass`).*

