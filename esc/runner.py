"""Executes a Run: search the portal, then contact matching candidates.

Runs happen on a background thread rather than in a request, because Playwright
holds a browser open for minutes at a time. The thread reports progress by
writing to the Run row; the UI polls it.

Ordering rule that makes this safe to kill at any moment: the Outreach row is
claimed (status SENDING) **before** the portal is asked to send, and only then
upgraded to SENT. A crash, a kill switch or a server reload can therefore at
worst leave one send recorded that may not have completed — never a candidate
contacted twice.

Only one run may be in flight at a time. Two concurrent runs of the same
profile would each read the already-contacted set before either wrote to it,
and the unique constraint would stop the second *row* but not the second
*message*.
"""

from __future__ import annotations

import os

# Playwright's sync API runs an asyncio event loop under the hood, which trips
# Django's "synchronous-only" ORM guard even though our worker thread is truly
# blocking. Runs execute on a dedicated thread and never inside a request, so
# it is safe to tell Django to allow ORM calls here. Must be set before the
# first ORM call in this process.
os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', '1')

import random
import threading
import time
import traceback

from django.db import close_old_connections, transaction
from django.utils import timezone

from .models import Candidate, GlobalSettings, Outreach, Run
from .portal import adapter, auth
from .portal.adapter import AdapterNotConfigured
from .portal.auth import ProfileBusy, SessionExpired


class RunAborted(RuntimeError):
    """Cooperative stop — user pressed stop, or a cap was reached."""


class RunRefused(RuntimeError):
    """A run may not start: another one already holds the slot."""


def active_run() -> Run | None:
    """The run currently holding the slot, if any."""
    return Run.objects.filter(
        state__in=(Run.State.QUEUED, Run.State.RUNNING)
    ).order_by('pk').first()


def claim_slot(run_id: int) -> str | None:
    """Move this run into RUNNING, or explain why it may not.

    Claims first and checks second, breaking a tie by primary key, so two runs
    starting at the same instant cannot both proceed — and cannot both refuse
    each other either.
    """
    claimed = Run.objects.filter(
        pk=run_id, state=Run.State.QUEUED
    ).update(state=Run.State.RUNNING, started_at=timezone.now())
    if not claimed:
        return 'This run is no longer queued.'

    contender = (
        Run.objects.filter(state=Run.State.RUNNING)
        .exclude(pk=run_id).order_by('pk').first()
    )
    if contender and contender.pk < run_id:
        return f'Run #{contender.pk} is already running — only one run at a time.'
    return None


def start_run(profile, dry_run: bool = True) -> Run:
    """Create a queued Run and hand it to a worker thread.

    Raises :class:`RunRefused` if a run is already in flight.
    """
    if busy := active_run():
        raise RunRefused(
            f'Run #{busy.pk} ({busy.search_profile.name}) is still '
            f'{busy.get_state_display().lower()} — only one run at a time.'
        )
    run = Run.objects.create(search_profile=profile, dry_run=dry_run)
    thread = threading.Thread(
        target=execute_run, args=(run.pk,), name=f'esc-run-{run.pk}', daemon=True
    )
    thread.start()
    return run


def _stop_reason(run: Run) -> str | None:
    """Check the cooperative stop signals. Re-read from the DB every time."""
    fresh = Run.objects.filter(pk=run.pk).values('stop_requested').first()
    if fresh and fresh['stop_requested']:
        return 'Stop requested from the UI.'
    if GlobalSettings.load().paused:
        return 'Global kill switch is on.'
    return None


def execute_run(run_id: int) -> None:
    """Thread entry point. Never raises — every outcome lands in the Run row."""
    close_old_connections()
    try:
        run = Run.objects.select_related('search_profile', 'search_profile__template').get(
            pk=run_id
        )
    except Run.DoesNotExist:
        return

    if refusal := claim_slot(run.pk):
        Run.objects.filter(pk=run.pk).update(
            state=Run.State.STOPPED, finished_at=timezone.now(), error=refusal
        )
        run.append_log(f'Refused: {refusal}')
        close_old_connections()
        return
    run.state, run.started_at = Run.State.RUNNING, timezone.now()

    final_state = Run.State.DONE
    error = ''
    try:
        _execute(run)
    except SessionExpired as exc:
        final_state, error = Run.State.NEEDS_LOGIN, str(exc)
        run.append_log(f'NEEDS LOGIN: {exc}')
    except RunAborted as exc:
        final_state = Run.State.STOPPED
        run.append_log(f'Stopped: {exc}')
    except ProfileBusy as exc:
        final_state, error = Run.State.STOPPED, str(exc)
        run.append_log(f'Browser busy: {exc}')
    except AdapterNotConfigured as exc:
        final_state, error = Run.State.FAILED, str(exc)
        run.append_log(f'ADAPTER NOT CONFIGURED: {exc}')
    except Exception as exc:  # noqa: BLE001 - a thread must not die silently
        final_state, error = Run.State.FAILED, f'{exc}\n\n{traceback.format_exc()}'
        run.append_log(f'FAILED: {exc}')
    finally:
        Run.objects.filter(pk=run.pk).update(
            state=final_state, finished_at=timezone.now(), error=error
        )
        close_old_connections()


def _execute(run: Run) -> None:
    profile = run.search_profile
    mode = 'DRY RUN — nothing will be sent' if run.dry_run else 'LIVE RUN'
    run.append_log(f'{mode}. Profile "{profile.name}", placement {profile.pass_id}.')

    if reason := _stop_reason(run):
        raise RunAborted(reason)

    if not run.dry_run:
        _preflight_live(run, profile)

    # Wait rather than fail if the heartbeat happens to hold the browser.
    with auth.browser_context(wait_s=90) as page:
        refs = _collect(run, profile, page)
        Run.objects.filter(pk=run.pk).update(found_count=len(refs))
        run.found_count = len(refs)
        run.append_log(f'Search finished: {len(refs)} candidate(s) matched.')

        _act(run, profile, page, refs)

    run.append_log(
        f'Done. contacted={run.contacted_count} skipped={run.skipped_count} '
        f'failed={run.failed_count}'
    )


def _preflight_live(run: Run, profile) -> None:
    """Refuse a live run that is misconfigured, before opening a browser."""
    adapter.ensure_configured()
    if not profile.template:
        raise RunAborted('No message template is attached to this profile.')
    if profile.remaining_today() <= 0:
        raise RunAborted(
            f'Daily cap reached ({profile.sent_today()}/'
            f'{profile.effective_daily_cap()} sent today).'
        )


def _collect(run: Run, profile, page) -> list[adapter.CandidateRef]:
    """Drive the search wizard and read the results. Read-only — contacts nobody.

    The portal search is a multi-step POST form, not a URL you can paginate by
    incrementing ``?page=``. So this drives the wizard once, then loads the
    largest page size the portal offers (500) to gather results in one view.
    Multi-page pagination beyond 500 is a TODO: it could not be exercised
    because every recon search for this placement returned no candidates, so
    the pager selectors are unverified. ``max_pages`` still bounds the loop.
    """
    if reason := _stop_reason(run):
        raise RunAborted(reason)

    adapter.run_search(page, profile.pass_id, profile.filters)
    auth.guard(page)

    # The empty-results marker is authoritative — the pager caption always
    # shows a placeholder ("1 - 1 / 1") even with no results, so trust the
    # marker, not the count, for the "nothing matched" case.
    if page.locator(adapter.WIZARD['empty_marker']).count():
        run.append_log('Portal returned no matching candidates.')
        return []

    # Ask for the largest page size, if that control is present.
    try:
        big = page.locator('a[href*="items_per_page=500"]').first
        if big.count():
            big.click()
            page.wait_for_load_state('networkidle')
            page.wait_for_timeout(800)
            adapter._expand_results(page)
    except Exception:
        pass

    total = adapter.result_total(page)
    refs = adapter.parse_results(page)
    seen = {r.external_id: r for r in refs}
    run.append_log(f'Read {len(refs)} result row(s)' + (f' of {total} reported.' if total else '.'))
    if total and len(refs) < total:
        run.append_log(
            f'NOTE: read {len(refs)} of {total}; pagination past one page is '
            'not yet implemented (needs a populated search to verify).'
        )
    return list(seen.values())


def _act(run: Run, profile, page, refs: list[adapter.CandidateRef]) -> None:
    already = Outreach.already_contacted_ids(profile)
    daily_left = profile.remaining_today()
    run_left = profile.per_run_cap
    template = profile.template

    for ref in refs:
        if reason := _stop_reason(run):
            raise RunAborted(reason)

        if ref.external_id in already:
            _bump(run, 'skipped_count')
            continue

        if run_left <= 0:
            run.append_log(f'Per-run cap ({profile.per_run_cap}) reached — stopping.')
            break
        if not run.dry_run and daily_left <= 0:
            run.append_log('Daily cap reached — stopping.')
            break

        candidate = _upsert_candidate(ref)

        if run.dry_run:
            _record(run, profile, candidate, Outreach.Status.DRY_RUN, template,
                    'Would contact.')
            run.append_log(f'[dry] would contact {candidate}')
            _bump(run, 'contacted_count')
            run_left -= 1
            continue

        subject = adapter.render_template(template.subject, ref)
        body = adapter.render_template(template.body, ref)

        # Claim BEFORE sending: see module docstring. If the process dies
        # between here and the upgrade below, the row survives as SENDING and
        # blocks this person from ever being contacted again by this profile.
        _record(run, profile, candidate, Outreach.Status.SENDING, template,
                'Send started; outcome not yet confirmed.', sent=True)

        try:
            result = adapter.send_outreach(page, ref, subject, body)
        except SessionExpired:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad row must not end the run
            result = adapter.OutreachResult(False, f'Error: {exc}')

        if result.ok:
            _record(run, profile, candidate, Outreach.Status.SENT, template,
                    result.detail, sent=True)
            run.append_log(f'Contacted {candidate}')
            _bump(run, 'contacted_count')
            daily_left -= 1
        else:
            _record(run, profile, candidate, Outreach.Status.FAILED, template,
                    result.detail)
            run.append_log(f'FAILED {candidate}: {result.detail}')
            _bump(run, 'failed_count')

        run_left -= 1
        time.sleep(random.uniform(profile.min_delay_s, profile.max_delay_s))


def _upsert_candidate(ref: adapter.CandidateRef) -> Candidate:
    candidate, _ = Candidate.objects.update_or_create(
        external_id=ref.external_id,
        defaults={
            'display_name': ref.display_name[:255],
            'profile_url': ref.profile_url[:500],
        },
    )
    return candidate


def _record(run, profile, candidate, status, template, detail, sent=False) -> None:
    """Write the outreach row, never duplicating one.

    ``update_or_create`` on the unique (profile, candidate) pair upgrades a
    DRY_RUN or retries a FAILED row in place. A SENT row is never overwritten,
    and an unresolved claim is never talked down to a dry-run preview — only a
    real send outcome, or a person saying what happened, may settle it.
    """
    with transaction.atomic():
        existing = Outreach.objects.filter(
            search_profile=profile, candidate=candidate
        ).first()
        if existing and existing.status in Outreach.TERMINAL:
            return
        if (
            existing
            and existing.status in Outreach.UNRESOLVED
            and status == Outreach.Status.DRY_RUN
        ):
            return
        Outreach.objects.update_or_create(
            search_profile=profile,
            candidate=candidate,
            defaults={
                'run': run,
                'template': template,
                'status': status,
                'detail': detail[:2000],
                'sent_at': timezone.now() if sent else None,
            },
        )


def _bump(run: Run, field: str) -> None:
    from django.db.models import F

    Run.objects.filter(pk=run.pk).update(**{field: F(field) + 1})
    setattr(run, field, getattr(run, field) + 1)
