"""Tests for the parts that must not be wrong.

The portal itself cannot be tested from here — it is behind EU Login and its
markup is unknown. What *is* tested is everything that decides whether someone
gets contacted: the dedupe guarantee, the caps, the kill switch, and the
refusal to act while the adapter is unconfigured.
"""

import contextlib
import datetime
import json
import pathlib
import shutil
import tempfile
import time
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import runner
from .models import Candidate, GlobalSettings, MessageTemplate, Outreach, Run, SearchProfile
from .portal import adapter, auth, heartbeat, login_manager
from .portal.auth import SessionExpired


def make_profile(**kwargs):
    defaults = {
        'name': 'Romania 2026',
        'pass_id': '82413',
        'filters': {'country': 'RO'},
        'per_run_cap': 5,
        'per_day_cap': 10,
        'min_delay_s': 0,
        'max_delay_s': 0,
    }
    return SearchProfile.objects.create(**{**defaults, **kwargs})


class SearchUrlTests(TestCase):
    def test_search_url_targets_the_placement_page(self):
        url = auth.search_url('82413')
        self.assertEqual(
            url, 'https://youth.europa.eu/admin/esc/pass/82413/search_en'
        )

    def test_candidate_id_extraction_falls_back_rather_than_dropping(self):
        self.assertEqual(adapter.extract_candidate_id('/en/participant/4471'), '4471')
        self.assertEqual(adapter.extract_candidate_id('/weird/path/99812'), '99812')
        self.assertEqual(adapter.extract_candidate_id(''), 'unknown')

    def test_pager_total_regex(self):
        self.assertEqual(adapter.PAGER_RE.search('1 - 50 / 137').group(3), '137')


class TemplateRenderTests(TestCase):
    def test_placeholders_substituted(self):
        ref = adapter.CandidateRef(external_id='7', display_name='Ana Pop')
        out = adapter.render_template('Hi {first_name} ({id})', ref)
        self.assertEqual(out, 'Hi Ana ({})'.format('7'))

    def test_stray_brace_does_not_raise(self):
        ref = adapter.CandidateRef(external_id='7', display_name='Ana')
        self.assertEqual(adapter.render_template('50% {off', ref), '50% {off')


class AdapterGuardTests(TestCase):
    def test_unconfigured_adapter_refuses_and_explains(self):
        self.assertFalse(adapter.is_configured())
        with self.assertRaises(adapter.AdapterNotConfigured) as ctx:
            adapter.ensure_configured()
        self.assertIn('esc_inspect', str(ctx.exception))


class LogTests(TestCase):
    def test_append_log_concatenates_rather_than_adding(self):
        """SQLite reads F('log') + str as numeric addition — Concat is required."""
        run = Run.objects.create(search_profile=make_profile())
        run.append_log('first')
        run.append_log('second')
        run.refresh_from_db()
        self.assertIn('first', run.log)
        self.assertIn('second', run.log)


class DedupeTests(TestCase):
    def setUp(self):
        self.profile = make_profile()
        self.candidate = Candidate.objects.create(external_id='4471', display_name='Ana')

    def test_sent_blocks_future_contact(self):
        Outreach.objects.create(
            search_profile=self.profile, candidate=self.candidate,
            status=Outreach.Status.SENT, sent_at=timezone.now(),
        )
        self.assertEqual(
            Outreach.already_contacted_ids(self.profile), {'4471'}
        )

    def test_dry_run_row_does_not_block_a_later_live_run(self):
        Outreach.objects.create(
            search_profile=self.profile, candidate=self.candidate,
            status=Outreach.Status.DRY_RUN,
        )
        self.assertEqual(Outreach.already_contacted_ids(self.profile), set())

    def test_failed_row_may_be_retried(self):
        Outreach.objects.create(
            search_profile=self.profile, candidate=self.candidate,
            status=Outreach.Status.FAILED,
        )
        self.assertEqual(Outreach.already_contacted_ids(self.profile), set())

    def test_record_upgrades_in_place_and_never_duplicates(self):
        from .runner import _record

        run = Run.objects.create(search_profile=self.profile)
        _record(run, self.profile, self.candidate, Outreach.Status.DRY_RUN, None, 'x')
        _record(run, self.profile, self.candidate, Outreach.Status.SENT, None, 'y',
                sent=True)
        rows = Outreach.objects.filter(
            search_profile=self.profile, candidate=self.candidate
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().status, Outreach.Status.SENT)

    def test_sent_is_terminal_and_not_downgraded(self):
        from .runner import _record

        run = Run.objects.create(search_profile=self.profile)
        _record(run, self.profile, self.candidate, Outreach.Status.SENT, None, 'y',
                sent=True)
        _record(run, self.profile, self.candidate, Outreach.Status.FAILED, None, 'z')
        row = Outreach.objects.get(search_profile=self.profile, candidate=self.candidate)
        self.assertEqual(row.status, Outreach.Status.SENT)


class CapTests(TestCase):
    def test_daily_cap_counts_only_sent_today(self):
        profile = make_profile(per_day_cap=3)
        for i in range(2):
            c = Candidate.objects.create(external_id=f'c{i}')
            Outreach.objects.create(
                search_profile=profile, candidate=c,
                status=Outreach.Status.SENT, sent_at=timezone.now(),
            )
        self.assertEqual(profile.sent_today(), 2)
        self.assertEqual(profile.remaining_today(), 1)

    def test_dry_run_rows_do_not_consume_the_daily_cap(self):
        profile = make_profile(per_day_cap=3)
        c = Candidate.objects.create(external_id='c9')
        Outreach.objects.create(
            search_profile=profile, candidate=c, status=Outreach.Status.DRY_RUN
        )
        self.assertEqual(profile.remaining_today(), 3)

    def test_global_ceiling_overrides_a_generous_profile(self):
        settings_row = GlobalSettings.load()
        settings_row.hard_daily_ceiling = 5
        settings_row.save()
        profile = make_profile(per_day_cap=10_000)
        self.assertEqual(profile.effective_daily_cap(), 5)


class RunnerTests(TestCase):
    def test_missing_session_lands_in_needs_login_not_a_crash(self):
        from .runner import execute_run
        from .portal import auth

        profile = make_profile()
        run = Run.objects.create(search_profile=profile, dry_run=True)
        # Force "no session" regardless of what is on disk — neither the state
        # file nor the browser profile.
        with mock.patch.object(auth, 'has_any_session', return_value=False):
            execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.state, Run.State.NEEDS_LOGIN)
        self.assertIn('esc_login', run.error)

    def test_live_run_refuses_while_adapter_unconfigured(self):
        from .runner import execute_run

        template = MessageTemplate.objects.create(name='t', subject='s', body='b')
        profile = make_profile(template=template)
        run = Run.objects.create(search_profile=profile, dry_run=False)
        execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.state, Run.State.FAILED)
        self.assertIn('esc_inspect', run.error)

    def test_live_run_without_template_is_stopped_before_any_browser_opens(self):
        from .runner import execute_run

        profile = make_profile()
        run = Run.objects.create(search_profile=profile, dry_run=False)
        with mock.patch.object(adapter, 'ensure_configured'):
            execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.state, Run.State.STOPPED)
        self.assertIn('template', run.log)

    def test_kill_switch_stops_a_run_immediately(self):
        from .runner import execute_run

        row = GlobalSettings.load()
        row.paused = True
        row.save()
        run = Run.objects.create(search_profile=make_profile(), dry_run=True)
        execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.state, Run.State.STOPPED)
        self.assertIn('kill switch', run.log.lower())


class ViewTests(TestCase):
    def test_all_pages_render(self):
        make_profile()
        for name in [
            'esc:dashboard', 'esc:run_list', 'esc:outreach_log',
            'esc:candidate_list', 'esc:template_list', 'esc:settings',
            'esc:profile_new', 'esc:template_new',
        ]:
            with self.subTest(view=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_dashboard_warns_that_the_adapter_is_unconfigured(self):
        response = self.client.get(reverse('esc:dashboard'))
        self.assertContains(response, 'not configured')

    def test_live_run_blocked_while_adapter_unconfigured(self):
        profile = make_profile()
        response = self.client.post(
            reverse('esc:run_start', args=[profile.pk]),
            {'mode': 'live', 'confirm': 'yes'}, follow=True,
        )
        self.assertContains(response, 'adapter is not configured')
        self.assertEqual(Run.objects.count(), 0)

    def test_live_run_blocked_without_explicit_confirmation(self):
        profile = make_profile()
        with mock.patch.object(adapter, 'is_configured', return_value=True):
            response = self.client.post(
                reverse('esc:run_start', args=[profile.pk]),
                {'mode': 'live'}, follow=True,
            )
        self.assertContains(response, 'not confirmed')
        self.assertEqual(Run.objects.count(), 0)

    def test_live_run_blocked_by_kill_switch(self):
        row = GlobalSettings.load()
        row.paused = True
        row.save()
        profile = make_profile()
        with mock.patch.object(adapter, 'is_configured', return_value=True):
            response = self.client.post(
                reverse('esc:run_start', args=[profile.pk]),
                {'mode': 'live', 'confirm': 'yes'}, follow=True,
            )
        self.assertContains(response, 'kill switch')
        self.assertEqual(Run.objects.count(), 0)

    # The bundled schema is always present, so the profile form uses the
    # generated filter fields. A valid post must satisfy the portal-required
    # fields (funding programme, dates, duration, activity topics).
    VALID_FILTERS = {
        'fp': '5',
        'period[start]': '2026-09-01',
        'period[end]': '2027-06-30',
        'period[duration]': '2',
        'projects[]': 'edu',
        'country': 'RO',
    }

    def _profile_post(self, **overrides):
        data = {
            'name': 'Test', 'pass_id': '82413',
            'per_run_cap': 5, 'per_day_cap': 10,
            'min_delay_s': 1, 'max_delay_s': 2, 'max_pages': 3,
            'enabled': 'on',
            **self.VALID_FILTERS,
        }
        data.update(overrides)
        return self.client.post(reverse('esc:profile_new'), data)

    def test_creating_a_profile_stores_wizard_ready_filters(self):
        response = self._profile_post()
        self.assertEqual(response.status_code, 302)
        profile = SearchProfile.objects.get(name='Test')
        self.assertEqual(profile.filters['country'], 'RO')
        self.assertEqual(profile.filters['projects[]'], 'edu')
        # Dates are stored as yyyy-mm-dd strings for the wizard driver.
        self.assertEqual(profile.filters['period[start]'], '2026-09-01')

    def test_profile_rejected_when_required_portal_field_missing(self):
        response = self._profile_post(**{'projects[]': ''})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SearchProfile.objects.filter(name='Test').exists())

    def test_per_day_cap_above_the_global_ceiling_is_rejected(self):
        response = self._profile_post(name='Greedy', per_day_cap=999_999)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SearchProfile.objects.filter(name='Greedy').exists())

    def test_inverted_delay_range_is_rejected(self):
        response = self._profile_post(name='Backwards', min_delay_s=30, max_delay_s=2)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SearchProfile.objects.filter(name='Backwards').exists())

    def test_stop_request_is_recorded(self):
        run = Run.objects.create(search_profile=make_profile(), state=Run.State.RUNNING)
        self.client.post(reverse('esc:run_stop', args=[run.pk]))
        run.refresh_from_db()
        self.assertTrue(run.stop_requested)


class PortalLoginViewTests(TestCase):
    def test_login_button_starts_interactive_login(self):
        from .portal import login_manager

        with mock.patch.object(login_manager, 'start', return_value=True) as start:
            response = self.client.post(
                reverse('esc:portal_login'), {'pass_id': '82413'}, follow=True
            )
        start.assert_called_once_with('82413')
        self.assertContains(response, 'browser window is opening')

    def test_login_falls_back_to_default_pass_id_when_blank(self):
        from .portal import login_manager

        SearchProfile.objects.create(name='P', pass_id='99999')
        with mock.patch.object(login_manager, 'start', return_value=True) as start:
            self.client.post(reverse('esc:portal_login'), {'pass_id': ''})
        start.assert_called_once_with('99999')

    def test_login_reports_when_already_running(self):
        from .portal import login_manager

        with mock.patch.object(login_manager, 'start', return_value=False):
            response = self.client.post(
                reverse('esc:portal_login'), {'pass_id': '82413'}, follow=True
            )
        self.assertContains(response, 'already in progress')

    def test_status_endpoint_is_json(self):
        from .portal import login_manager

        with mock.patch.object(login_manager, 'status_json',
                               return_value={'running': False, 'has_session': False}):
            response = self.client.get(reverse('esc:portal_login_status'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['running'], False)

    def test_settings_page_shows_login_button(self):
        response = self.client.get(reverse('esc:settings'))
        # Label depends on whether a session already exists; the form is what
        # matters.
        self.assertContains(response, reverse('esc:portal_login'))
        self.assertContains(response, 'name="pass_id"')


class FilterSchemaTests(TestCase):
    def test_bundled_schema_is_available(self):
        fields = adapter.load_filter_schema()
        names = {f['name'] for f in fields}
        self.assertIn('projects[]', names)
        self.assertIn('country', names)
        self.assertIn('period[start]', names)

    def test_funding_programme_field_present(self):
        fp = adapter.funding_programme_field()
        self.assertEqual(fp['name'], 'fp')
        self.assertTrue(fp['choices'])

    def test_ui_builds_real_fields_from_the_bundled_schema(self):
        response = self.client.get(reverse('esc:profile_new'))
        self.assertContains(response, 'name="country"')
        self.assertContains(response, 'name="projects[]"')
        self.assertContains(response, 'Activity topics')

    def test_ui_falls_back_to_raw_json_when_schema_is_empty(self):
        with mock.patch.object(adapter, 'load_filter_schema', return_value=[]):
            response = self.client.get(reverse('esc:profile_new'))
        self.assertContains(response, 'Filters (JSON)')


# ---------------------------------------------------------------------------
# Session health, read from cookies rather than a file timestamp
# ---------------------------------------------------------------------------

class SessionStatusTests(TestCase):
    """A file's mtime cannot tell "signed in" from "expired weeks ago"."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = self.dir / 'storage_state.json'
        # A profile dir that does not exist, so only the state file counts.
        self.profile = self.dir / 'no-such-profile'

    def write_state(self, portal_expires, eulogin_expires=None):
        cookies = [{
            'name': 'SSESSad5ef963', 'value': 'x', 'domain': '.youth.europa.eu',
            'path': '/', 'expires': portal_expires,
        }]
        if eulogin_expires is not None:
            cookies.append({
                'name': auth.EULOGIN_COOKIE, 'value': 'x',
                'domain': 'ecas.ec.europa.eu', 'path': '/',
                'expires': eulogin_expires,
            })
        self.state.write_text(json.dumps({'cookies': cookies, 'origins': []}),
                              encoding='utf-8')

    def status(self):
        with override_settings(ESC_STORAGE_STATE=self.state,
                               ESC_BROWSER_PROFILE=self.profile):
            return auth.session_status()

    def test_no_session_at_all(self):
        self.assertEqual(self.status()['state'], 'none')

    def test_a_live_cookie_reads_as_ok(self):
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=10)
        self.write_state(soon.timestamp())
        status = self.status()
        self.assertEqual(status['state'], 'ok')
        self.assertEqual(status['portal_until'].date(), soon.date())

    def test_a_cookie_expiring_within_the_warning_window(self):
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=6)
        self.write_state(soon.timestamp())
        self.assertEqual(self.status()['state'], 'expiring')

    def test_a_dead_cookie_reads_as_expired_not_as_signed_in(self):
        past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)
        self.write_state(past.timestamp())
        self.assertEqual(self.status()['state'], 'expired')

    def test_the_eulogin_horizon_is_reported_separately(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        self.write_state((now - datetime.timedelta(days=1)).timestamp(),
                         (now + datetime.timedelta(days=30)).timestamp())
        status = self.status()
        self.assertEqual(status['state'], 'expired')
        self.assertGreater(status['eulogin_until'], now)

    def test_a_lapsed_portal_session_with_a_live_ticket_is_recoverable(self):
        # What a lapsed session actually looks like on disk: Chrome drops the
        # expired portal cookie, leaving only the EU Login ticket — which is
        # exactly what silent SSO recovers from.
        self.state.write_text(json.dumps({'cookies': [{
            'name': '__Secure-CASTGC', 'value': 'x',
            'domain': 'webgate.ec.europa.eu', 'path': '/', 'expires': -1,
        }], 'origins': []}), encoding='utf-8')
        self.assertEqual(self.status()['state'], 'recoverable')

    def test_session_scoped_cookies_are_not_treated_as_expired(self):
        # Playwright writes -1 for a session cookie; it lives as long as the
        # state file is replayed, which is exactly what this tool does.
        self.write_state(-1)
        status = self.status()
        self.assertEqual(status['state'], 'ok')
        self.assertIsNone(status['portal_until'])


# ---------------------------------------------------------------------------
# Silent SSO recovery
# ---------------------------------------------------------------------------

class StubLocator:
    def __init__(self, present):
        self._present = present

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self._present else 0

    def is_visible(self, timeout=None):
        return self._present


class StubPage:
    """Just enough of a Playwright page for auth.guard()."""

    def __init__(self, urls, login_form=False, title=''):
        self._urls = list(urls)
        self._login_form = login_form
        self._title = title

    def title(self):
        return self._title

    @property
    def url(self):
        # The last URL is sticky: it is where the browser settled.
        return self._urls[0] if len(self._urls) == 1 else self._urls.pop(0)

    def is_closed(self):
        return False

    def wait_for_timeout(self, ms):
        time.sleep(min(ms, 20) / 1000)

    def locator(self, selector):
        return StubLocator(self._login_form)


PORTAL = 'https://youth.europa.eu/admin/esc/pass/82413/search_en'
ECAS = 'https://ecas.ec.europa.eu/cas/login?authenticationLevel=MEDIUM'


class SsoRecoveryTests(TestCase):
    def test_portal_content_passes_straight_through(self):
        auth.guard(StubPage([PORTAL]))  # must not raise

    def test_a_bounce_that_lands_back_on_the_portal_is_not_an_error(self):
        # The EU Login ticket is still valid: ECAS redirects us home without a
        # form. Failing here is what used to make a one-time login weekly.
        auth.guard(StubPage([ECAS, PORTAL]), timeout_s=5)

    def test_a_real_credential_form_means_the_session_is_gone(self):
        with self.assertRaises(SessionExpired) as caught:
            auth.guard(StubPage([ECAS], login_form=True), timeout_s=5)
        self.assertIn('esc_login', str(caught.exception))

    def test_a_block_page_is_not_waited_out(self):
        # ECAS turns non-browser clients away with a "Sorry" page. It never
        # redirects anywhere, so sitting on it for the full timeout is delay
        # for its own sake.
        page = StubPage(['https://youth.europa.eu/eulogin_en?destination=/admin'],
                        title='Sorry - 6429012')
        started = time.monotonic()
        with self.assertRaises(SessionExpired):
            auth.guard(page, timeout_s=10)
        self.assertLess(time.monotonic() - started, 5)

    def test_stuck_on_login_eventually_gives_up(self):
        with self.assertRaises(SessionExpired):
            auth.guard(StubPage([ECAS]), timeout_s=0.3)

    def test_recovery_can_be_switched_off(self):
        with self.assertRaises(SessionExpired):
            auth.guard(StubPage([ECAS, PORTAL]), recover=False)


# ---------------------------------------------------------------------------
# Claim before send: the no-double-contact promise, at the ordering level
# ---------------------------------------------------------------------------

class ClaimBeforeSendTests(TestCase):
    def setUp(self):
        self.template = MessageTemplate.objects.create(
            name='t', subject='s', body='Hello there'
        )
        self.profile = make_profile(template=self.template)
        self.run = Run.objects.create(
            search_profile=self.profile, dry_run=False, state=Run.State.RUNNING
        )
        self.refs = [adapter.CandidateRef(external_id='c1', display_name='Ana B')]

    def act(self):
        from .runner import _act

        _act(self.run, self.profile, None, self.refs)

    def test_a_kill_mid_send_leaves_a_claim_that_blocks_re_contact(self):
        # KeyboardInterrupt stands in for a hard kill: _act catches Exception,
        # not BaseException, so nothing tidies up after this.
        with mock.patch.object(adapter, 'send_outreach', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.act()

        row = Outreach.objects.get()
        self.assertEqual(row.status, Outreach.Status.SENDING)
        self.assertIn('c1', Outreach.already_contacted_ids(self.profile))

    def test_the_claim_is_upgraded_when_the_send_succeeds(self):
        with mock.patch.object(adapter, 'send_outreach',
                               return_value=adapter.OutreachResult(True, 'ok')):
            self.act()
        row = Outreach.objects.get()
        self.assertEqual(row.status, Outreach.Status.SENT)
        self.assertIsNotNone(row.sent_at)

    def test_a_clean_failure_releases_the_person_for_a_retry(self):
        with mock.patch.object(adapter, 'send_outreach',
                               return_value=adapter.OutreachResult(False, 'nope')):
            self.act()
        row = Outreach.objects.get()
        self.assertEqual(row.status, Outreach.Status.FAILED)
        self.assertIsNone(row.sent_at)
        self.assertNotIn('c1', Outreach.already_contacted_ids(self.profile))

    def test_an_unconfirmed_claim_spends_the_daily_cap(self):
        with mock.patch.object(adapter, 'send_outreach', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.act()
        self.assertEqual(self.profile.sent_today(), 1)

    def test_a_dry_run_cannot_talk_a_claim_back_down(self):
        from .runner import _record

        candidate = Candidate.objects.create(external_id='c1')
        Outreach.objects.create(
            search_profile=self.profile, candidate=candidate,
            status=Outreach.Status.SENDING, sent_at=timezone.now(),
        )
        _record(self.run, self.profile, candidate, Outreach.Status.DRY_RUN,
                self.template, 'Would contact.')
        self.assertEqual(Outreach.objects.get().status, Outreach.Status.SENDING)

    def test_resolving_a_claim_as_not_sent_releases_it(self):
        candidate = Candidate.objects.create(external_id='c1', display_name='Ana B')
        row = Outreach.objects.create(
            search_profile=self.profile, candidate=candidate,
            status=Outreach.Status.SENDING, sent_at=timezone.now(),
        )
        response = self.client.post(
            reverse('esc:outreach_resolve', args=[row.pk]), {'outcome': 'not_sent'}
        )
        self.assertEqual(response.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.status, Outreach.Status.FAILED)
        self.assertNotIn('c1', Outreach.already_contacted_ids(self.profile))

    def test_resolving_a_claim_as_sent_makes_it_terminal(self):
        candidate = Candidate.objects.create(external_id='c1')
        row = Outreach.objects.create(
            search_profile=self.profile, candidate=candidate,
            status=Outreach.Status.SENDING, sent_at=timezone.now(),
        )
        self.client.post(reverse('esc:outreach_resolve', args=[row.pk]),
                         {'outcome': 'sent'})
        row.refresh_from_db()
        self.assertEqual(row.status, Outreach.Status.SENT)


# ---------------------------------------------------------------------------
# One run at a time
# ---------------------------------------------------------------------------

class RunSlotTests(TestCase):
    def test_start_run_refuses_while_another_run_is_in_flight(self):
        profile = make_profile()
        Run.objects.create(search_profile=profile, state=Run.State.RUNNING)
        with self.assertRaises(runner.RunRefused):
            runner.start_run(profile, dry_run=True)
        self.assertEqual(Run.objects.count(), 1)  # nothing queued behind it

    def test_execute_run_stands_down_if_it_loses_the_race(self):
        profile = make_profile()
        holder = Run.objects.create(search_profile=profile, state=Run.State.RUNNING)
        loser = Run.objects.create(search_profile=profile, dry_run=True)
        runner.execute_run(loser.pk)
        loser.refresh_from_db()
        self.assertEqual(loser.state, Run.State.STOPPED)
        self.assertIn(str(holder.pk), loser.error)

    def test_the_reaper_frees_a_slot_held_by_a_dead_process(self):
        run = Run.objects.create(search_profile=make_profile(), state=Run.State.RUNNING)
        Run.reap_interrupted()
        run.refresh_from_db()
        self.assertEqual(run.state, Run.State.FAILED)
        self.assertIn('Interrupted', run.error)
        self.assertIsNone(runner.active_run())

    def test_the_dashboard_disables_running_while_a_run_is_active(self):
        profile = make_profile()
        Run.objects.create(search_profile=profile, state=Run.State.RUNNING)
        html = self.client.get(reverse('esc:dashboard')).content.decode()
        self.assertIn('one run at a time', html)


# ---------------------------------------------------------------------------
# Keep-alive
# ---------------------------------------------------------------------------

class HeartbeatTests(TestCase):
    """The heartbeat must never be the thing that breaks a run."""

    def test_skipped_while_a_run_holds_the_browser(self):
        Run.objects.create(search_profile=make_profile(), state=Run.State.RUNNING)
        with mock.patch.object(auth, 'has_any_session', return_value=True):
            self.assertIn('run', heartbeat.tick())

    def test_skipped_when_there_is_nothing_to_keep_alive(self):
        with mock.patch.object(auth, 'has_any_session', return_value=False):
            self.assertIn('No saved session', heartbeat.tick())

    def test_skipped_while_a_login_window_is_open(self):
        with mock.patch.object(auth, 'has_any_session', return_value=True), \
             mock.patch.object(login_manager, 'is_running', return_value=True):
            self.assertIn('login', heartbeat.tick())

    @override_settings(ESC_HEARTBEAT_ENABLED=False)
    def test_disabled_by_setting(self):
        self.assertIn('disabled', heartbeat.tick().lower())
        self.assertFalse(heartbeat.start())

    def test_a_healthy_check_is_recorded(self):
        page = StubPage([PORTAL])

        @contextlib.contextmanager
        def fake_context(**kwargs):
            yield page

        with mock.patch.object(auth, 'has_any_session', return_value=True), \
             mock.patch.object(auth, 'browser_context', fake_context), \
             mock.patch.object(auth, 'goto_portal', lambda p, url, **kw: p):
            self.assertEqual(heartbeat.tick(), 'ok')
        self.assertIsNotNone(heartbeat.last_ok_at())

    def test_an_expired_session_is_reported_not_raised(self):
        @contextlib.contextmanager
        def fake_context(**kwargs):
            raise SessionExpired('gone')
            yield  # pragma: no cover

        with mock.patch.object(auth, 'has_any_session', return_value=True), \
             mock.patch.object(auth, 'browser_context', fake_context):
            self.assertEqual(heartbeat.tick(), 'expired')


class ProfileLockTests(TestCase):
    def test_the_browser_profile_is_not_handed_out_twice(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        with override_settings(ESC_BROWSER_PROFILE=d):
            with auth.profile_lock():
                with self.assertRaises(auth.ProfileBusy):
                    with auth.profile_lock():
                        pass  # pragma: no cover
            # released again afterwards
            with auth.profile_lock():
                pass
