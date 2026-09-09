from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import runner
from .forms import (
    GlobalSettingsForm,
    MessageTemplateForm,
    RawFiltersForm,
    SearchProfileForm,
    build_filter_form,
    filters_to_json,
)
from .models import Candidate, GlobalSettings, MessageTemplate, Outreach, Run, SearchProfile
from .portal import adapter, auth, heartbeat, login_manager


def _portal_status() -> dict:
    """Health banner data: is the session good, and is the adapter usable.

    ``session`` comes from the cookies themselves, so "signed in" and "signed
    in three weeks ago and long since expired" no longer look identical.
    """
    session = auth.session_status()
    return {
        'has_session': session['state'] != 'none',
        'session': session,
        'session_ok': session['state'] in ('ok', 'expiring', 'recoverable'),
        'session_saved_at': session['saved_at'],
        'heartbeat': heartbeat.status_json(),
        # The raw datetime, so the template can format it; status_json
        # stringifies for JSON consumers.
        'heartbeat_last_ok': heartbeat.last_ok_at(),
        'adapter_configured': adapter.is_configured(),
        'adapter_missing': adapter.unconfigured_keys(),
        'schema_available': adapter.schema_is_available(),
        'paused': GlobalSettings.load().paused,
        'unresolved': Outreach.objects.filter(
            status__in=Outreach.UNRESOLVED
        ).count(),
        'active_run': runner.active_run(),
    }


def dashboard(request):
    profiles = SearchProfile.objects.annotate(
        sent_total=Count('outreach', filter=Q(outreach__status=Outreach.Status.SENT)),
    ).prefetch_related('runs')
    rows = [
        {
            'profile': p,
            'last_run': p.runs.first(),
            'sent_total': p.sent_total,
            'remaining_today': p.remaining_today(),
        }
        for p in profiles
    ]
    return render(request, 'esc/dashboard.html', {
        'rows': rows,
        'active_runs': Run.objects.filter(
            state__in=[Run.State.QUEUED, Run.State.RUNNING]
        ).select_related('search_profile'),
        'status': _portal_status(),
    })


def profile_edit(request, pk=None):
    profile = get_object_or_404(SearchProfile, pk=pk) if pk else None
    schema = adapter.load_filter_schema()
    existing = profile.filters if profile else {}

    funding_field = adapter.funding_programme_field()

    if request.method == 'POST':
        form = SearchProfileForm(request.POST, instance=profile)
        if schema:
            filter_form = build_filter_form(
                schema, data=request.POST, funding_field=funding_field
            )
            raw_form = None
        else:
            filter_form = None
            raw_form = RawFiltersForm(request.POST)

        active = filter_form or raw_form
        if form.is_valid() and active.is_valid():
            obj = form.save(commit=False)
            if filter_form:
                obj.filters = filters_to_json(filter_form.cleaned_data)
            else:
                obj.filters = raw_form.cleaned_data['filters']
            obj.save()
            messages.success(request, f'Saved profile "{obj.name}".')
            return redirect('esc:dashboard')
    else:
        form = SearchProfileForm(instance=profile)
        if schema:
            filter_form = build_filter_form(
                schema, initial=existing, funding_field=funding_field
            )
            raw_form = None
        else:
            filter_form = None
            raw_form = RawFiltersForm(
                initial={'filters': _pretty(existing)}
            )

    return render(request, 'esc/profile_form.html', {
        'form': form,
        'filter_form': filter_form,
        'raw_form': raw_form,
        'profile': profile,
        'status': _portal_status(),
    })


def _pretty(data) -> str:
    import json
    return json.dumps(data, indent=2) if data else ''


@require_POST
def profile_delete(request, pk):
    profile = get_object_or_404(SearchProfile, pk=pk)
    name = profile.name
    profile.delete()
    messages.success(request, f'Deleted profile "{name}".')
    return redirect('esc:dashboard')


@require_POST
def run_start(request, pk):
    profile = get_object_or_404(SearchProfile, pk=pk)
    live = request.POST.get('mode') == 'live'

    if not profile.enabled:
        messages.error(request, 'That profile is disabled.')
        return redirect('esc:dashboard')

    if live:
        # Every reason a live run must not start, checked before a browser opens.
        if GlobalSettings.load().paused:
            messages.error(request, 'The global kill switch is on. Nothing was sent.')
            return redirect('esc:dashboard')
        if request.POST.get('confirm') != 'yes':
            messages.error(request, 'Live run not confirmed.')
            return redirect('esc:dashboard')
        if not adapter.is_configured():
            messages.error(
                request,
                'The portal adapter is not configured yet — run esc_inspect and '
                'fill in SELECTORS before any live run.',
            )
            return redirect('esc:dashboard')
        if not profile.template:
            messages.error(request, 'Attach a message template first.')
            return redirect('esc:profile_edit', pk=profile.pk)

    try:
        run = runner.start_run(profile, dry_run=not live)
    except runner.RunRefused as exc:
        messages.error(request, str(exc))
        return redirect('esc:dashboard')
    return redirect('esc:run_detail', pk=run.pk)


def run_detail(request, pk):
    run = get_object_or_404(Run.objects.select_related('search_profile'), pk=pk)
    return render(request, 'esc/run_detail.html', {
        'run': run,
        'outreach': run.outreach.select_related('candidate')[:200],
        'status': _portal_status(),
    })


def run_status(request, pk):
    """Polled by run_detail.html to stream progress without a page reload."""
    run = get_object_or_404(Run, pk=pk)
    return JsonResponse({
        'state': run.state,
        'state_display': run.get_state_display(),
        'active': run.is_active,
        'dry_run': run.dry_run,
        'found': run.found_count,
        'contacted': run.contacted_count,
        'skipped': run.skipped_count,
        'failed': run.failed_count,
        'log': run.log,
        'error': run.error,
        'needs_login': run.state == Run.State.NEEDS_LOGIN,
    })


@require_POST
def run_stop(request, pk):
    run = get_object_or_404(Run, pk=pk)
    Run.objects.filter(pk=run.pk).update(stop_requested=True)
    messages.info(request, 'Stop requested — the run halts after the current candidate.')
    return redirect('esc:run_detail', pk=run.pk)


def run_list(request):
    return render(request, 'esc/run_list.html', {
        'runs': Run.objects.select_related('search_profile')[:200],
        'status': _portal_status(),
    })


def outreach_log(request):
    qs = Outreach.objects.select_related('candidate', 'search_profile', 'run')
    status = request.GET.get('status')
    profile_id = request.GET.get('profile')
    if status:
        qs = qs.filter(status=status)
    if profile_id:
        qs = qs.filter(search_profile_id=profile_id)
    return render(request, 'esc/outreach_log.html', {
        'outreach': qs[:500],
        'statuses': Outreach.Status.choices,
        'profiles': SearchProfile.objects.all(),
        'current_status': status,
        'current_profile': profile_id,
        'status': _portal_status(),
    })


@require_POST
def outreach_resolve(request, pk):
    """Settle a claim the runner could not confirm.

    A SENDING row means the portal was asked to send and the process died
    before the answer came back. Only a human looking at the portal can say
    which it was, so the app refuses to guess: until then the person stays
    blocked from further contact.
    """
    row = get_object_or_404(Outreach, pk=pk)
    if row.status not in Outreach.UNRESOLVED:
        messages.warning(request, 'That outreach row is not awaiting a decision.')
        return redirect('esc:outreach_log')

    outcome = request.POST.get('outcome')
    if outcome == 'sent':
        row.status = Outreach.Status.SENT
        row.detail = 'Confirmed sent by hand.'
        messages.success(request, f'{row.candidate} marked as contacted.')
    elif outcome == 'not_sent':
        row.status = Outreach.Status.FAILED
        row.sent_at = None
        row.detail = 'Confirmed NOT sent by hand; may be retried.'
        messages.success(
            request, f'{row.candidate} released — a later run may contact them.'
        )
    else:
        messages.error(request, 'No outcome given.')
        return redirect('esc:outreach_log')
    row.save(update_fields=['status', 'sent_at', 'detail'])
    return redirect('esc:outreach_log')


def candidate_list(request):
    qs = Candidate.objects.prefetch_related('outreach__search_profile')
    if q := request.GET.get('q'):
        qs = qs.filter(Q(display_name__icontains=q) | Q(external_id__icontains=q))
    return render(request, 'esc/candidate_list.html', {
        'candidates': qs[:300],
        'q': request.GET.get('q', ''),
        'status': _portal_status(),
    })


def template_list(request):
    return render(request, 'esc/template_list.html', {
        'templates': MessageTemplate.objects.all(),
        'status': _portal_status(),
    })


def template_edit(request, pk=None):
    obj = get_object_or_404(MessageTemplate, pk=pk) if pk else None
    if request.method == 'POST':
        form = MessageTemplateForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, 'Template saved.')
            return redirect('esc:template_list')
    else:
        form = MessageTemplateForm(instance=obj)
    return render(request, 'esc/template_form.html', {
        'form': form, 'template_obj': obj, 'status': _portal_status(),
    })


def _default_pass_id() -> str:
    return login_manager.default_pass_id()


def settings_view(request):
    obj = GlobalSettings.load()
    if request.method == 'POST':
        form = GlobalSettingsForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, 'Settings saved.')
            return redirect('esc:settings')
    else:
        form = GlobalSettingsForm(instance=obj)
    pass_id = _default_pass_id()
    return render(request, 'esc/settings.html', {
        'form': form,
        'status': _portal_status(),
        'default_pass_id': pass_id,
        'vnc_url': settings.ESC_VNC_URL if settings.ESC_VNC_ENABLED else '',
        'login_running': login_manager.is_running(),
        'login_command': f'python manage.py esc_login --pass-id {pass_id}',
        'inspect_command': f'python manage.py esc_inspect --pass-id {pass_id} --capture-action',
    })


@require_POST
def portal_login(request):
    pass_id = (request.POST.get('pass_id') or '').strip() or _default_pass_id()
    if login_manager.start(pass_id):
        messages.info(
            request,
            'A browser window is opening — complete EU Login there (including '
            '2FA). This page will update once you are signed in.',
        )
    else:
        messages.warning(request, 'A login is already in progress.')
    return redirect('esc:settings')


def portal_login_status(request):
    return JsonResponse(login_manager.status_json())


@require_POST
def toggle_pause(request):
    obj = GlobalSettings.load()
    obj.paused = not obj.paused
    obj.save()
    messages.warning(
        request,
        'Kill switch ON — no run can contact anyone.' if obj.paused
        else 'Kill switch off. Runs may contact candidates again.',
    )
    return redirect(request.POST.get('next') or reverse('esc:dashboard'))
