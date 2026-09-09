"""Open a real browser so the user can sign in to EU Login by hand."""

from django.core.management.base import BaseCommand, CommandError

from esc.portal import auth


class Command(BaseCommand):
    help = (
        'Open a browser window, wait for you to complete EU Login, then save '
        'the session for later headless runs. No password is ever handled or '
        'stored by this tool.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--pass-id', required=True,
            help='Placement id from the portal URL, e.g. 82413.',
        )
        parser.add_argument(
            '--timeout', type=int, default=600,
            help='Seconds to wait for you to finish logging in (default 600).',
        )

    def handle(self, *args, **options):
        try:
            path = auth.interactive_login(
                options['pass_id'],
                timeout_s=options['timeout'],
                echo=self.stdout.write,
            )
        except TimeoutError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f'\nSession saved: {path}'))
        self.stdout.write(
            'Next:  python manage.py esc_inspect --pass-id '
            f'{options["pass_id"]} --capture-action'
        )
