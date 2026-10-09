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

The suite also has to mean the same thing wherever it runs. Settings resolve
the environment before their defaults (ConvAI/site_config.py), and on a server
the environment is that installation's .env — from compose's env_file, or from
the load_dotenv() calls in settings.py, urls.py and utils.py. A real
SUMMARY_MODEL or PLATFORM_PHONE then overrode what the tests expected, and the
deploy check failed on code that was fine. So before the settings load, every
app setting is taken out of the environment and .env files are not read: a test
run sees what a checkout without any .env sees. Tests that need a value set it
themselves (override_settings, mock.patch.dict(os.environ, ...)).

MEETINGS_APP is the one exception — it chooses which apps are installed, and
running the suite with the meetings app removed is deliberate:

    MEETINGS_APP=0 python3 manage.py test ConvAI --settings=test_settings
"""

import os
import re
from pathlib import Path

import dotenv

_HERE = Path(__file__).resolve().parent
# Read on purpose, from the environment of the run, not from a file.
_KEEP = {"MEETINGS_APP", "DJANGO_SETTINGS_MODULE"}
# Older spellings site_config still falls back to (its _LEGACY_ENV).
_LEGACY = {"EMAIL_HOST", "EMAIL_PORT", "EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD", "DEFAULT_FROM_EMAIL"}


def _app_setting_names():
    """Every name the app reads from the environment: .env.sample, plus the
    keys site_config resolves (some predate the sample)."""
    names = set(_LEGACY)
    sample = _HERE / ".env.sample"
    if sample.exists():
        names |= set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", sample.read_text(), re.M))
    site_config = _HERE / "ConvAI" / "site_config.py"
    if site_config.exists():
        names |= set(re.findall(r'"([A-Z][A-Z0-9_]{2,})"\s*:', site_config.read_text()))
    return names - _KEEP


for _name in _app_setting_names():
    os.environ.pop(_name, None)

# Every later `from dotenv import load_dotenv` (settings.py, urls.py, utils.py)
# picks this up, so no .env lying next to the code is read back in.
dotenv.load_dotenv = lambda *args, **kwargs: False

os.environ.setdefault("DB_ENGINE", "django.db.backends.sqlite3")

from Django_CMS.settings import *  # noqa: F401,F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
MIGRATION_MODULES = {"ConvAI": None, "meetings": None}
