"""Role model built on Django groups + a permission.

Roles:
- Admin    = superuser (holds the ``access_configuration`` permission) → everything.
- Navigator = ``Navigator`` group, non-staff → own clients only, no configuration.
- Test user = ``PatientTester`` group, linked 1:1 to a Patient → audio chatbot only.
Clients are ``Patient`` rows, never users.
"""
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import user_passes_test

NAVIGATOR = "Navigator"
PATIENT_TESTER = "PatientTester"

CONFIG_PERM = "ConvAI.access_configuration"


def is_admin(user):
    """Admins can access configuration and see all clients."""
    return bool(user and user.is_authenticated and user.has_perm(CONFIG_PERM))


def is_navigator(user):
    return bool(
        user and user.is_authenticated
        and (is_admin(user) or user.groups.filter(name=NAVIGATOR).exists())
    )


def is_tester(user):
    return bool(
        user and user.is_authenticated
        and (is_admin(user) or user.groups.filter(name=PATIENT_TESTER).exists())
    )


def admin_required(view):
    return user_passes_test(is_admin, login_url=settings.LOGIN_URL)(view)


def navigator_required(view):
    return user_passes_test(is_navigator, login_url=settings.LOGIN_URL)(view)


def tester_required(view):
    return user_passes_test(is_tester, login_url=settings.LOGIN_URL)(view)
