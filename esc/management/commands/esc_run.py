"""Run a search profile from the command line (for cron / Task Scheduler).

Dry run is the default. A live run needs ``--live``, and if the profile would
contact more than a handful of people it also needs ``--yes``.
"""

from django.core.management.base import BaseCommand, CommandError

from esc.models import Run, SearchProfile
from esc.runner import execute_run


class Command(BaseCommand):
    help = 'Execute a search profile, in the foreground.'

    def add_arguments(self, parser):
        parser.add_argument('--profile', required=True, help='SearchProfile name.')
        parser.add_argument(
            '--live', action='store_true',
            help='Actually contact candidates. Without this it is a dry run.',
        )
        parser.add_argument(
            '--yes', action='store_true',
            help='Skip the confirmation prompt for a live run.',
        )

    def handle(self, *args, **options):
        try:
            profile = SearchProfile.objects.get(name=options['profile'])
        except SearchProfile.DoesNotExist as exc:
            names = ', '.join(SearchProfile.objects.values_list('name', flat=True))
            raise CommandError(
                f'No profile named "{options["profile"]}". Known: {names or "(none)"}'
            ) from exc

        if not profile.enabled:
            raise CommandError(f'Profile "{profile.name}" is disabled.')

        # A run row left RUNNING by a killed server would hold the only slot.
        if reaped := Run.reap_interrupted():
            self.stdout.write(self.style.WARNING(
                f'Marked {reaped} interrupted run(s) as failed before starting.'
            ))

        live = options['live']
        if live and not options['yes']:
            self.stdout.write(self.style.WARNING(
                f'\nLIVE RUN — this contacts real people.\n'
                f'  profile:       {profile.name}\n'
                f'  placement:     {profile.pass_id}\n'
                f'  per-run cap:   {profile.per_run_cap}\n'
                f'  left today:    {profile.remaining_today()} '
                f'of {profile.effective_daily_cap()}\n'
            ))
            if input('Type "yes" to continue: ').strip().lower() != 'yes':
                raise CommandError('Aborted.')

        run = Run.objects.create(search_profile=profile, dry_run=not live)
        self.stdout.write(f'Run #{run.pk} started ({"live" if live else "dry"}).')

        # Foreground on purpose: a scheduled task should block until finished.
        execute_run(run.pk)

        run.refresh_from_db()
        self.stdout.write(run.log or '(no log)')

        summary = (
            f'Run #{run.pk} {run.state}: found={run.found_count} '
            f'contacted={run.contacted_count} skipped={run.skipped_count} '
            f'failed={run.failed_count}'
        )
        if run.state in (Run.State.DONE, Run.State.STOPPED):
            self.stdout.write(self.style.SUCCESS(summary))
        else:
            self.stdout.write(self.style.ERROR(summary))
            if run.error:
                self.stdout.write(run.error)
