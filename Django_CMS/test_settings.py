"""Settings for running the test suite locally, on sqlite.

The project talks to Postgres and reads its connection out of the environment,
so `manage.py test` has nowhere to build a test database without one. This gives
it one in memory.

ConvAI's migration 0022 runs raw Postgres SQL, so the migration chain cannot be
replayed on sqlite at all. Tables are built straight from the models instead —
which is what MIGRATION_MODULES = {"ConvAI": None} means — and any migration
worth testing is exercised by calling it directly; see
ConvAI/test_call_attribution.py, which does that for the recording backfill.

    python3 manage.py test ConvAI --settings=test_settings
"""

import os

os.environ.setdefault("DB_ENGINE", "django.db.backends.sqlite3")

from Django_CMS.settings import *  # noqa: F401,F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
MIGRATION_MODULES = {"ConvAI": None}
