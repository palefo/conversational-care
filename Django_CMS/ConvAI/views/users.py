import secrets
from ._base import *  # noqa: F401,F403
from ..forms import NavigatorForm, TestUserForm, EditUserForm
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils.text import slugify

__all__ = ['user_list', 'create_user', 'create_test_user', 'test_user_result', 'edit_user']


def _user_role_label(u):
    """Human-readable role for a user, derived from superuser flag + groups."""
    gnames = {g.name for g in u.groups.all()}
    if u.is_superuser:
        return _("Admin")
    if 'Navigator' in gnames:
        return _("Navigator")
    if 'PatientTester' in gnames:
        return _("Test user")
    return "—"


@login_required
@admin_required
def user_list(request):
    """Admin-only listing of application users, split into two tabs.

    * ``users``   — admins and navigators (staff-facing accounts).
    * ``testers`` — PatientTester accounts, each linked to a client.

    The active tab is paginated and sortable; the querystring carries ``tab``,
    ``sort``, ``dir``, ``page`` and ``per_page``.
    """
    User = get_user_model()

    tab = request.GET.get('tab', 'users')
    if tab not in ('users', 'testers'):
        tab = 'users'

    testers_qs = (
        User.objects
        .filter(groups__name='PatientTester')
        .prefetch_related('groups')
        .select_related('test_patient')
        .distinct()
    )
    # "Users" = everyone who is not solely a test user (admins + navigators).
    users_qs = (
        User.objects
        .exclude(groups__name='PatientTester')
        .prefetch_related('groups')
        .distinct()
    )

    users_count = users_qs.count()
    testers_count = testers_qs.count()

    # Sorting — only DB-backed columns are sortable.
    if tab == 'testers':
        sort_map = {
            'username': ['username'],
            'email': ['email'],
            'client': ['test_patient__name', 'test_patient__lastname'],
            'status': ['is_active'],
        }
        qs = testers_qs
    else:
        sort_map = {
            'username': ['username'],
            'email': ['email'],
            'status': ['is_active'],
        }
        qs = users_qs

    sort = request.GET.get('sort', 'username')
    if sort not in sort_map:
        sort = 'username'
    direction = request.GET.get('dir', 'asc')
    if direction not in ('asc', 'desc'):
        direction = 'asc'
    prefix = '-' if direction == 'desc' else ''
    order_fields = [f'{prefix}{field}' for field in sort_map[sort]]
    if 'username' not in sort_map[sort]:
        order_fields.append('username')  # stable tiebreaker
    qs = qs.order_by(*order_fields)

    # Pagination.
    allowed_per_page = [10, 25, 50, 100]
    try:
        per_page = int(request.GET.get('per_page', 25))
    except (TypeError, ValueError):
        per_page = 25
    if per_page not in allowed_per_page:
        per_page = 25

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get('page'))

    page_range = list(
        paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1)
    )

    rows = []
    for u in page_obj:
        rows.append({
            'obj': u,
            'role': _user_role_label(u),
            'client': getattr(u, 'test_patient', None),
        })

    # Base querystrings (mirrors the calls list pattern) so pagination, per-page
    # and sort links preserve the active tab and each other.
    base_params = request.GET.copy()
    base_params.pop('page', None)
    base_params['tab'] = tab
    base_query = base_params.urlencode()

    pp_params = base_params.copy()
    pp_params.pop('per_page', None)
    base_query_no_pp = pp_params.urlencode()

    sort_params = base_params.copy()
    sort_params.pop('sort', None)
    sort_params.pop('dir', None)
    sort_base = sort_params.urlencode()

    return render(request, 'users/user_list.html', {
        'active_page': 'users',
        'tab': tab,
        'rows': rows,
        'page_obj': page_obj,
        'per_page': per_page,
        'per_page_options': allowed_per_page,
        'page_range': page_range,
        'ellipsis': paginator.ELLIPSIS,
        'total_count': paginator.count,
        'users_count': users_count,
        'testers_count': testers_count,
        'base_query': base_query,
        'base_query_no_pp': base_query_no_pp,
        'sort_base': sort_base,
        'current_sort': sort,
        'current_dir': direction,
        'navigator_form': NavigatorForm(),
        'test_user_form': TestUserForm(),
    })


@login_required
@admin_required
@require_POST
def create_user(request):
    """Create a Navigator account (admin only). Username + password are set here."""
    form = NavigatorForm(request.POST)
    if form.is_valid():
        cd = form.cleaned_data
        user = get_user_model()(
            username=cd['username'], email=cd.get('email') or "",
            is_staff=False, is_active=True,
        )
        user.set_password(cd['password'])
        user.save()
        user.groups.add(Group.objects.get(name='Navigator'))
        messages.success(request, _("Navigator user created."))
    else:
        errs = "; ".join(f"{f}: {', '.join(e)}" for f, e in form.errors.items())
        messages.error(request, _("Could not create user.") + " " + errs)
    return redirect('users')


def _unique_test_username(patient):
    """Generate a readable, collision-free username for a client's test user."""
    User = get_user_model()
    base = slugify(f"{patient.name}-{patient.lastname}") or "client"
    base = ("test-" + base)[:40].rstrip("-")
    for _attempt in range(20):
        candidate = f"{base}-{secrets.token_hex(2)}"
        if not User.objects.filter(username__iexact=candidate).exists():
            return candidate
    return f"test-{uuid.uuid4().hex[:12]}"


@login_required
@admin_required
@require_POST
def create_test_user(request):
    """Create a Test-user account for a client with generated credentials.

    A client may have at most one test user. On success the (one-time) generated
    username and password are stashed in the session and shown on a result page.
    """
    form = TestUserForm(request.POST)
    if not form.is_valid():
        errs = "; ".join(f"{f}: {', '.join(e)}" for f, e in form.errors.items())
        messages.error(request, _("Could not create test user.") + " " + errs)
        return redirect(f"{reverse('users')}?tab=testers")

    patient = form.cleaned_data['patient']
    username = _unique_test_username(patient)
    password = secrets.token_urlsafe(12)

    User = get_user_model()
    user = User(username=username, is_staff=False, is_active=True)
    user.set_password(password)
    user.save()
    user.groups.add(Group.objects.get(name='PatientTester'))
    patient.tester_account = user
    patient.save(update_fields=['tester_account'])

    # Show the credentials exactly once via the result page (not persisted).
    request.session['test_user_creds'] = {
        'username': username,
        'password': password,
        'patient_id': patient.pk,
    }
    return redirect('test_user_result')


@login_required
@admin_required
def test_user_result(request):
    """One-time display of a freshly generated test user's credentials.

    Also surfaces which agent is configured for the client and offers a shortcut
    to edit the client (e.g. to set/adjust the agent).
    """
    creds = request.session.pop('test_user_creds', None)
    if not creds:
        return redirect(f"{reverse('users')}?tab=testers")

    patient = Patient.objects.select_related('agent').filter(pk=creds.get('patient_id')).first()
    return render(request, 'users/test_user_result.html', {
        'active_page': 'users',
        'username': creds['username'],
        'password': creds['password'],
        'patient': patient,
        'agent': getattr(patient, 'agent', None) if patient else None,
    })


@login_required
@admin_required
def edit_user(request, pk):
    """Edit an existing account (username, e-mail, active) — admin only."""
    user = get_object_or_404(get_user_model(), pk=pk)
    if request.method == 'POST':
        form = EditUserForm(request.POST, instance=user)
        if form.is_valid():
            form.save()
            messages.success(request, _("User updated."))
            return redirect('users')
    else:
        form = EditUserForm(instance=user)
    return render(request, 'users/edit_user.html', {
        'active_page': 'users',
        'form': form,
        'edited_user': user,
        'role': _user_role_label(user),
    })
