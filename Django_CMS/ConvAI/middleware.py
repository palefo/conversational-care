"""Per-user language activation.

Runs after AuthenticationMiddleware (so request.user is available) and after
LocaleMiddleware, so an authenticated user's stored preference wins over the
system default. Users without a preference fall back to whatever LocaleMiddleware
selected (the system default).
"""
from django.utils import translation


class UserLanguageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        lang = ""
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            lang = getattr(user, "preferred_language", "") or ""

        if lang:
            translation.activate(lang)
            request.LANGUAGE_CODE = lang

        response = self.get_response(request)

        if lang:
            response.setdefault("Content-Language", lang)
        return response
