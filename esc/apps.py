"""App wiring for the background work that keeps a long-lived server honest.

Only ``runserver`` gets this: a management command, a shell or a system check
must never quietly open a browser or rewrite run rows as a side effect of
being started. It runs on a thread rather than inside ``ready()`` so that app
initialisation stays free of database queries.
"""

import os
import sys
import threading

from django.apps import AppConfig
from django.conf import settings


class EscConfig(AppConfig):
    name = 'esc'

    def ready(self):
        if not self._should_start():
            return
        threading.Thread(
            target=self._startup, name='esc-startup', daemon=True
        ).start()

    @staticmethod
    def _should_start() -> bool:
        if os.environ.get('ESC_START_BACKGROUND') == '1':
            return True
        if 'runserver' not in sys.argv:
            return False
        # Under the autoreloader only the child process (RUN_MAIN set) should
        # do this work; with --noreload there is no child and we are it.
        return os.environ.get('RUN_MAIN') == 'true' or '--noreload' in sys.argv

    def _startup(self):
        from django.db import close_old_connections

        from .models import Run
        from .portal import heartbeat

        close_old_connections()
        try:
            count = Run.reap_interrupted()
        except Exception as exc:  # noqa: BLE001 - unmigrated db, mid-deploy, etc.
            print(f'esc: could not check for interrupted runs ({exc}).')
        else:
            if count:
                print(f'esc: marked {count} interrupted run(s) as failed.')
        finally:
            close_old_connections()

        if heartbeat.start():
            print(
                'esc: keeping the portal session warm every '
                f'{settings.ESC_HEARTBEAT_INTERVAL}s.'
            )
