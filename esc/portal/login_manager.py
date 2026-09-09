"""Drive the interactive EU Login from the web UI.

:func:`start` launches a headed browser on a background thread (the same
``interactive_login`` the CLI uses), so clicking a button in Settings opens the
real EU Login window for the user to complete by hand. Progress is kept in a
small in-memory state the UI polls — no password is ever handled here.

State is process-local, which is exactly right for the single-process dev
server this tool runs under.
"""

from __future__ import annotations

import threading

from django.utils import timezone

from . import auth

_lock = threading.Lock()
_state: dict = {
    'running': False,
    'started_at': None,
    'finished_at': None,
    'success': None,
    'message': '',
    'error': '',
    'pass_id': None,
}


def default_pass_id() -> str:
    """The placement to open when nobody named one.

    The last one signed in with, else the first saved profile's — the tool is
    almost always pointed at a single placement, so guessing well beats asking.
    """
    with _lock:
        last = _state['pass_id']
    if last:
        return str(last)

    from ..models import SearchProfile

    profile = SearchProfile.objects.exclude(pass_id='').first()
    return profile.pass_id if profile else '82413'


def is_running() -> bool:
    with _lock:
        return _state['running']


def status_json() -> dict:
    with _lock:
        s = dict(_state)
    for key in ('started_at', 'finished_at'):
        if s[key] is not None:
            s[key] = s[key].isoformat()
    s['has_session'] = auth.has_any_session()
    return s


def start(pass_id: str, timeout_s: int = 600, persistent: bool = True) -> bool:
    """Begin an interactive login. Returns False if one is already running."""
    with _lock:
        if _state['running']:
            return False
        _state.update(
            running=True, started_at=timezone.now(), finished_at=None,
            success=None, error='', pass_id=pass_id,
            message='Opening your browser — complete EU Login there.',
        )
    threading.Thread(
        target=_run, args=(pass_id, timeout_s, persistent),
        name='esc-login', daemon=True,
    ).start()
    return True


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def _echo(line) -> None:
    line = (line or '').strip()
    if line:
        _set(message=line)


def _run(pass_id: str, timeout_s: int, persistent: bool = True) -> None:
    try:
        auth.interactive_login(
            pass_id, timeout_s=timeout_s, echo=_echo, persistent=persistent
        )
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        _set(running=False, finished_at=timezone.now(), success=False,
             error=str(exc), message='Login did not complete.')
        return

    # "…then do the rest": confirm the saved session actually reaches the
    # portal headlessly, so the user knows the app is ready to run.
    _echo('Signed in. Verifying the session works headlessly…')
    try:
        # Wait for the browser: the heartbeat may have taken it the
        # instant the login window closed, and "busy" is not "expired".
        ok = auth.verify_session(pass_id, wait_s=90)
    except Exception as exc:  # noqa: BLE001
        _set(running=False, finished_at=timezone.now(), success=False,
             error=str(exc), message='Signed in, but the session check errored.')
        return

    if ok:
        _set(running=False, finished_at=timezone.now(), success=True,
             message='Signed in and verified — the app is ready to run.')
    else:
        _set(running=False, finished_at=timezone.now(), success=False,
             message='Signed in, but the headless check was redirected to login. '
                     'Try again.')
