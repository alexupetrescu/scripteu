"""Keep the portal session warm so the hand sign-in stays a one-time step.

The portal runs Drupal's autologout module (``Drupal.visitor.autologout_login``
is in its cookie jar), so an idle session is dropped long before its cookie's
stated expiry. A quiet page load every ``ESC_HEARTBEAT_INTERVAL`` seconds keeps
it alive and — because :func:`auth.browser_context` exports on the way out —
re-saves the refreshed cookies each time.

The tick is deliberately timid. It skips itself entirely whenever a run is in
flight, a login window is open, the browser profile is locked by another
process, or there is no session to keep alive. It never contacts anyone: it
loads the search page and leaves.

State is process-local, which is right for the single-process dev server this
tool runs under.
"""

from __future__ import annotations

import os

# Same reason as runner.py: Playwright's sync API spins an asyncio loop under a
# thread that is genuinely blocking, which trips Django's async-safety guard.
os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', '1')

import threading  # noqa: E402
import traceback  # noqa: E402

from django.conf import settings  # noqa: E402
from django.db import close_old_connections  # noqa: E402
from django.utils import timezone  # noqa: E402

from . import auth, login_manager  # noqa: E402

_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_state: dict = {
    'running': False,
    'last_tick_at': None,
    'last_ok_at': None,
    'result': '',
    'error': '',
}


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def status_json() -> dict:
    with _lock:
        s = dict(_state)
    for key in ('last_tick_at', 'last_ok_at'):
        if s[key] is not None:
            s[key] = s[key].isoformat()
    s['interval'] = settings.ESC_HEARTBEAT_INTERVAL
    s['enabled'] = settings.ESC_HEARTBEAT_ENABLED
    return s


def last_ok_at():
    with _lock:
        return _state['last_ok_at']


def _run_in_flight() -> bool:
    from ..models import Run

    close_old_connections()
    return Run.objects.filter(
        state__in=(Run.State.QUEUED, Run.State.RUNNING)
    ).exists()


def _skip_reason() -> str | None:
    """Why this tick should do nothing. Cheap checks first."""
    if not settings.ESC_HEARTBEAT_ENABLED:
        return 'Heartbeat disabled.'
    if login_manager.is_running():
        return 'A login window is open.'
    if not auth.has_any_session():
        return 'No saved session to keep alive.'
    try:
        if _run_in_flight():
            return 'A run is using the browser.'
    except Exception as exc:  # noqa: BLE001 - never let a tick kill the thread
        return f'Could not check for active runs: {exc}'
    return None


def tick() -> str:
    """One keep-alive pass. Returns a one-line result, and never raises."""
    _set(last_tick_at=timezone.now())

    if reason := _skip_reason():
        _set(result=f'Skipped — {reason}', error='')
        return reason

    pass_id = login_manager.default_pass_id()
    try:
        with auth.browser_context(headless=True, wait_s=0) as page:
            auth.goto_portal(page, auth.search_url(pass_id))
            ok = not auth.is_login_url(page.url)
    except auth.ProfileBusy as exc:
        _set(result=f'Skipped — {exc}', error='')
        return str(exc)
    except auth.SessionExpired as exc:
        _set(result='Session has expired — sign in again.', error=str(exc))
        return 'expired'
    except Exception as exc:  # noqa: BLE001 - a daemon thread must not die
        _set(result='Keep-alive failed.', error=f'{exc}\n\n{traceback.format_exc()}')
        return f'error: {exc}'
    finally:
        close_old_connections()

    if ok:
        now = timezone.now()
        _set(last_ok_at=now, result='Session healthy.', error='')
        return 'ok'
    _set(result='Landed on EU Login — the session needs a hand sign-in.', error='')
    return 'needs login'


def _loop() -> None:
    interval = settings.ESC_HEARTBEAT_INTERVAL
    # A first tick soon after startup, so Settings shows something real before
    # the full interval has elapsed — but not instantly, to stay out of the
    # way of a server that is still booting.
    if _stop.wait(30):
        return
    while not _stop.is_set():
        tick()
        if _stop.wait(interval):
            return


def start() -> bool:
    """Start the heartbeat thread. Returns False if it is already running."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        if not settings.ESC_HEARTBEAT_ENABLED:
            return False
        _stop.clear()
        _state['running'] = True
        _thread = threading.Thread(target=_loop, name='esc-heartbeat', daemon=True)
        _thread.start()
    return True


def stop() -> None:
    """Ask the thread to finish. Used by tests; the daemon dies with the process."""
    _stop.set()
    _set(running=False)
