from ._base import *  # noqa: F401,F403
from django.contrib.auth import views as auth_views
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import HttpResponseRedirect
from django.urls import reverse_lazy
from ..forms import HelpContentForm
from ..default_help import DEFAULT_HELP_MARKDOWN
from ..site_config import brand_name

__all__ = ['RoleBasedLoginView', '_issue_user_token', 'issue_api_token', 'profile',
           'help_page', 'help_edit', 'update_language', 'update_profile',
           'PasswordResetRequestView', 'PasswordResetSentView',
           'PasswordResetConfirmView', 'PasswordResetCompleteView']


def _profile_role_label(user):
    """Human-readable role for the profile header."""
    if is_admin(user):
        return _("Administrator")
    if is_navigator(user):
        return _("Counsellor")
    if is_tester(user):
        return _("Test user")
    return _("User")


@login_required
def help_page(request):
    """Render the Help page from admin-editable Markdown.

    Falls back to the shipped default content until an admin saves their own.
    The Edit button is only offered to admins.
    """
    cfg = SiteConfiguration.load()
    stored = (cfg.help_markdown or "").strip()
    return render(request, 'account/help.html', {
        'active_page': 'help',
        'help_markdown': stored or DEFAULT_HELP_MARKDOWN,
        'is_default': not stored,
        'can_edit': is_admin(request.user),
    })


@admin_required
def help_edit(request):
    """Admin-only Markdown editor for the Help page content."""
    cfg = SiteConfiguration.load()
    if request.method == 'POST':
        form = HelpContentForm(request.POST, instance=cfg)
        if form.is_valid():
            form.save()  # singleton save() also clears the config cache
            messages.success(request, _("Help page updated."))
            return redirect('help')
    else:
        # Prefill with the current content, or the shipped default when empty,
        # so admins start from real text instead of a blank editor.
        initial = (cfg.help_markdown or "").strip() or DEFAULT_HELP_MARKDOWN
        form = HelpContentForm(instance=cfg, initial={'help_markdown': initial})
    return render(request, 'account/help_edit.html', {
        'active_page': 'help',
        'form': form,
    })


def _issue_user_token(user) -> str:
    """
    Creates or regenerates a DRF Token for the given user.
    Returns the raw token string (display it once to the user).
    """
    # Remove existing token (acts as revoke)
    Token.objects.filter(user=user).delete()
    # Create a new one
    token = Token.objects.create(user=user)
    return token.key


@login_required
def profile(request):
    """
    Profile page: indicates whether a token exists and (optionally) shows
    a freshly issued token once if present in session.
    """
    token_exists = Token.objects.filter(user=request.user).exists()
    just = request.session.pop("just_issued_token", None)  # set this after issuing

    return render(request, "account/profile.html", {
        "active_page": "profile",
        "has_token": token_exists,
        "just_issued_token": just,  # None or the newly generated token
        "languages": settings.LANGUAGES,
        "role_label": _profile_role_label(request.user),
    })


@login_required
@require_POST
def update_profile(request):
    """Let the signed-in user edit their own name (and e-mail)."""
    user = request.user
    first = (request.POST.get("first_name") or "").strip()
    last = (request.POST.get("last_name") or "").strip()
    email = (request.POST.get("email") or "").strip()

    if email:
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, _("Please enter a valid e-mail address."))
            return redirect("profile")

    user.first_name = first
    user.last_name = last
    user.email = email
    user.save(update_fields=["first_name", "last_name", "email"])
    messages.success(request, _("Profile updated."))
    return redirect("profile")


@login_required
@require_POST
def update_language(request):
    """Save the current user's preferred interface language (blank = system default)."""
    lang = (request.POST.get("preferred_language") or "").strip()
    allowed = {code for code, _label in settings.LANGUAGES}
    if lang and lang not in allowed:
        lang = ""
    request.user.preferred_language = lang
    request.user.save(update_fields=["preferred_language"])
    messages.success(request, _("Language updated."))
    return redirect("profile")


@login_required
@require_POST
def issue_api_token(request):
    raw = _issue_user_token(request.user)
    # Store for one-time display on next render
    request.session["just_issued_token"] = raw
    messages.success(request, _("Your token has been generated. Copy it now; it will be shown only once."))
    return redirect("profile")


class RoleBasedLoginView(LoginView):
    """
    Redirect:
      • Staff / Navigator → dashboard
      • PatientTester     → external chat for their patient
    """
    LOCK_THRESHOLD = 5
    LOCK_WINDOW = 300  # seconds

    def _fail_key(self):
        ip = self.request.META.get("REMOTE_ADDR", "?")
        return f"login-fails:{ip}"

    def post(self, request, *args, **kwargs):
        # MC-002 fix: simple per-IP brute-force lockout.
        from django.core.cache import cache
        if cache.get(self._fail_key(), 0) >= self.LOCK_THRESHOLD:
            return HttpResponse("Too many failed login attempts. Try again later.", status=429)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        from django.core.cache import cache
        key = self._fail_key()
        cache.set(key, cache.get(key, 0) + 1, timeout=self.LOCK_WINDOW)
        return super().form_invalid(form)

    def form_valid(self, form):
        from django.core.cache import cache
        cache.delete(self._fail_key())
        return super().form_valid(form)

    def get_success_url(self):
        user = self.request.user

        if user.groups.filter(name="PatientTester").exists():
            patient = getattr(user, "test_patient", None)
            if patient:
                return reverse("external_chat")
            # tester without a linked patient – fall back
        # F9 fix: LOGIN_REDIRECT_URL is now a valid named route ('dashboard').
        return super().get_success_url()




# ── Password recovery ───────────────────────────────────────────────────────
# Django's own views do the work; these subclasses only fix the two things a
# stock install gets wrong here — the templates, and the host the link points
# at. The mail itself goes out through ConvAI.mailer.PlatformEmailBackend, so it
# travels over whichever provider Settings → Email selects.

class PasswordResetRequestView(auth_views.PasswordResetView):
    """Ask for the address, send the link."""
    template_name = "registration/password_reset_form.html"
    email_template_name = "registration/password_reset_email.txt"
    html_email_template_name = "registration/password_reset_email.html"
    subject_template_name = "registration/password_reset_subject.txt"
    success_url = reverse_lazy("password_reset_done")

    def form_valid(self, form):
        # django.contrib.sites is installed and SITE_ID is 1, so the stock view
        # builds the link against whatever that row says — "example.com" on an
        # install nobody edited it on, which produces a mail whose only link is
        # dead. The host the request actually arrived on is the one the person
        # reading the mail can click, and ALLOWED_HOSTS has already vetted it.
        form.save(
            domain_override=self.request.get_host(),
            use_https=self.request.is_secure(),
            token_generator=self.token_generator,
            from_email=self.from_email,
            email_template_name=self.email_template_name,
            html_email_template_name=self.html_email_template_name,
            subject_template_name=self.subject_template_name,
            extra_email_context={"brand_name": brand_name()},
            request=self.request,
        )
        return HttpResponseRedirect(self.get_success_url())


class PasswordResetSentView(auth_views.PasswordResetDoneView):
    """"We sent it" — worded so it does not reveal whether the address exists."""
    template_name = "registration/password_reset_done.html"


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    """The link's destination: choose the new password."""
    template_name = "registration/password_reset_confirm.html"
    success_url = reverse_lazy("password_reset_complete")


class PasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "registration/password_reset_complete.html"
