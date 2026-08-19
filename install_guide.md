# **Installation guide for Conversational Care**

This guide provides instructions for setting up the Conversational Care project in the `Django_CMS` folder for local development using Docker and PostgreSQL.

## Docker & PostgreSQL Setup (Development)

This setup uses Docker Compose to run the Django application and a PostgreSQL database in isolated containers.

### Prerequisites
* **Docker:** [Install Docker Desktop](https://www.docker.com/products/docker-desktop/)
* **Docker Compose:** (Included with Docker Desktop)

### Update Project Files

Before building the containers, you need to configure the Django project to work with PostgreSQL and environment variables.


### Build and Run the Application

1.  **Open a terminal** and navigate to the `Conversational-Care/Django_CMS/` directory and copy the `.env.sample` to `.env` and make the required adjustments.
    ```bash
    cd Django_CMS/
    cp .env.sample .env
    ```   

3.  **Build the containers:**
    ```bash
    docker-compose build
    ```
4.  **Start the services:**
    This command starts the `db` and `web` containers. The `web` service will execute the `CMD` from the `Dockerfile`, which is `python manage.py runserver 0.0.0.0:8000`.
    ```bash
    docker-compose up
    ```
    (You can add `-d` to run them in the background).

### Setup the Database (First-Time Only)

With the containers running, you must run the database migrations.

1.  **Open a *new* terminal** in the same directory.

2.  **Run migrations:**
    ```bash
    docker-compose exec web python manage.py migrate
    ```

3.  **Create a superuser (optional):**
    ```bash
    docker-compose exec web python manage.py createsuperuser
    ```

### Access the Site

Your Django application is now running.
* **URL:** [http://localhost:8000](http://localhost:8000)
* **Admin:** [http://localhost:8000/admin](http://localhost:8000/admin)

## Translations (i18n)

The interface is available in six languages: **en_GB** (British English),
**es_PE** (Peruvian Spanish), **pt_BR** (Brazilian Portuguese), **it**
(Italian), **ko** (Korean), and **zh_Hans** (Simplified Chinese). Translation
message IDs in the code are written in English; the translated strings live in
`locale/<code>/LC_MESSAGES/django.po`.

Choose the default language for a deployment with the `PLATFORM_LANG` environment
variable in `.env` (`PLATFORM_LANG=English`, `Spanish`, `Portuguese`, `Italian`,
`Korean`, or `Chinese`). Users can also pick their own interface language on
their Profile page.

After editing any `.po` file, recompile the binary `.mo` files and restart the
web service:

```bash
docker compose exec web python manage.py compilemessages
docker compose restart web
```

To regenerate the `.po` files after adding or changing `{% trans %}` /
`gettext` strings in the source:

```bash
docker compose exec web python manage.py makemessages -a --no-obsolete
```

> Both commands require the `gettext` package, which is installed in the `web`
> image via the `Dockerfile`.

