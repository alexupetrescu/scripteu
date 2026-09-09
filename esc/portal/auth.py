"""EU Login (ECAS) session handling.

The portal sits behind ECAS single sign-on at ``authenticationLevel=MEDIUM``,
and ECAS actively blocks non-browser clients (a plain HTTP fetch of the login
URL is redirected to ``sorry.ec.europa.eu``). So there is no scripted login
here, by design: the user authenticates by hand once in a real browser window,
and the resulting session is reused by later headless runs.

"Once" is only true because of three things in this module:

* **Two carriers, kept in sync.** The persistent browser profile holds the
  browser identity ECAS ties its 2FA device-trust to; ``storage_state.json``
  holds the session-scoped cookies (``__Secure-CASTGC``,
  ``__Secure-ECAS_SESSIONID``) that Chrome drops when a profile closes. Neither
  survives alone, so every launch injects the file into the profile and exports
  it again on the way out.
* **Silent SSO recovery.** The portal's Drupal session dies in weeks; the EU
  Login ticket outlives it. A bounce to EU Login is therefore usually recovered
  without a human, and only a real credential form means "sign in again".
* **Honest expiry.** Session health is read from the cookies themselves, not
  from a file's modification time.

Nothing in this module ever handles a password.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from playwright.sync_api import sync_playwright

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None
try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


class SessionExpired(RuntimeError):
    """The saved session is gone, and a human needs to log in again.

    Raised instead of letting a run fail somewhere downstream with an
    inscrutable "selector not found", which is what an expired cookie
    otherwise looks like.
    """


class ProfileBusy(RuntimeError):
    """The browser profile is already open elsewhere.

    Chrome allows one process per user-data-dir, and this app opens it from
    three places (a run, an interactive login, the heartbeat) across two
    processes (``runserver`` and the ``esc_run`` CLI).
    """


SIGN_IN_HINT = (
    'Open Settings -> "Open login in same browser", or run:  '
    'python manage.py esc_login --pass-id <id>'
)

# A real credential form: the one thing SSO cannot get past on its own. Checked
# for presence rather than visibility — EU Login renders its fields through
# JavaScript, and a field that exists but has not been painted yet still means
# a human is being asked to type.
LOGIN_FORM_SELECTOR = (
    'input[type="password"], #username, #password, '
    'input[name="username"], input[name="email"]'
)

# ECAS turns non-browser clients (and clients it is rate-limiting) away with a
# "Sorry" / "Server inaccessibility" page. It is not a login form and it will
# never redirect anywhere on its own, so waiting out the full timeout on one is
# pure delay. Observed against the live portal.
BLOCKED_PAGE_MARKERS = ('sorry', 'server inaccessibility', 'access denied')


def storage_state_path() -> Path:
    return Path(settings.ESC_STORAGE_STATE)


def has_session() -> bool:
    path = storage_state_path()
    if not path.exists():
        return False
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return False
    return bool(state.get('cookies'))


def session_saved_at() -> dt.datetime | None:
    """When the session was last written (its rough age)."""
    path = storage_state_path()
    candidates = [p for p in (path, profile_dir()) if p.exists()]
    if not candidates:
        return None
    newest = max(p.stat().st_mtime for p in candidates)
    return dt.datetime.fromtimestamp(newest, tz=dt.timezone.utc)


def profile_dir() -> Path:
    return Path(settings.ESC_BROWSER_PROFILE)


def has_profile() -> bool:
    """True if the persistent browser profile exists and has content.

    This is the durable half of the session — the profile the interactive login
    writes to, and the one every headless run reuses.
    """
    d = profile_dir()
    return d.exists() and any(d.iterdir())


def has_any_session() -> bool:
    return has_profile() or has_session()


def search_url(pass_id: str | int, path: str | None = None) -> str:
    template = path or settings.ESC_SEARCH_PATH
    return urljoin(settings.ESC_BASE_URL, template.format(pass_id=pass_id))


def is_login_url(url: str) -> bool:
    """True if this URL is an EU Login page rather than portal content."""
    if any(host in url for host in settings.ESC_LOGIN_HOSTS):
        return True
    return '/eulogin' in url or '/casservice' in url


# ---------------------------------------------------------------------------
# Session health, read from the cookies rather than a file timestamp
# ---------------------------------------------------------------------------

#: Drupal's session cookie on the portal — the one that actually expires.
PORTAL_COOKIE_PREFIX = 'SSESS'
#: EU Login's authentication-assurance cookie: the horizon for silent SSO.
EULOGIN_COOKIE = '__Secure-ECAS_AAP_1'
#: The EU Login ticket itself. Session-scoped, so it lives only in the state
#: file — and it is what makes a lapsed portal session recoverable.
TICKET_COOKIES = ('__Secure-CASTGC', '__Secure-ECAS_SESSIONID')


def _cookies() -> list[dict]:
    path = storage_state_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return []
    return data.get('cookies', []) if isinstance(data, dict) else []


def _expiry(cookie: dict) -> dt.datetime | None:
    """A cookie's expiry, or None when it is session-scoped.

    Playwright writes ``-1`` for a session cookie. Those do not expire on a
    clock; they live for as long as the storage state is reused, which is
    exactly what this tool does with them.
    """
    expires = cookie.get('expires', -1)
    if not expires or expires < 0:
        return None
    try:
        return dt.datetime.fromtimestamp(expires, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def session_status() -> dict:
    """Session health as the UI should present it.

    ``state`` is one of:

    ``none``         nothing saved — a hand sign-in is required.
    ``expired``      the portal session is gone and cannot be recovered.
    ``recoverable``  the portal session has lapsed, but the EU Login ticket is
                     still there, so the next run should re-authenticate
                     silently without asking you for anything.
    ``expiring``     the portal session dies within ``ESC_SESSION_WARN_DAYS``.
    ``ok``           good, as far as the cookies can tell.

    Cheap and offline: this never opens a browser. Only an actual run (or the
    heartbeat) can prove a session still works — but a file timestamp, which is
    what this replaced, could not even tell "signed in" from "expired weeks
    ago".
    """
    now = dt.datetime.now(tz=dt.timezone.utc)
    status = {
        'state': 'none',
        'portal_until': None,
        'eulogin_until': None,
        'saved_at': session_saved_at(),
        'has_profile': has_profile(),
        'has_state_file': has_session(),
    }
    if not has_any_session():
        return status

    portal, eulogin = [], []
    has_portal_cookie = has_ticket = False
    for cookie in _cookies():
        name = cookie.get('name', '')
        domain = cookie.get('domain', '')
        when = _expiry(cookie)
        alive = when is None or when > now  # None means session-scoped
        if name.startswith(PORTAL_COOKIE_PREFIX) and 'youth.europa.eu' in domain:
            has_portal_cookie = True
            if when is not None:
                portal.append(when)
        elif name == EULOGIN_COOKIE and when is not None:
            eulogin.append(when)
        elif name in TICKET_COOKIES and alive:
            has_ticket = True

    status['portal_until'] = min(portal) if portal else None
    status['eulogin_until'] = max(eulogin) if eulogin else None
    status['has_ticket'] = has_ticket

    horizon = status['portal_until']
    if horizon is not None and horizon <= now:
        status['state'] = 'expired'
    elif horizon is not None and horizon - now <= dt.timedelta(
        days=settings.ESC_SESSION_WARN_DAYS
    ):
        status['state'] = 'expiring'
    elif horizon is not None:
        status['state'] = 'ok'
    elif has_portal_cookie:
        # Session-scoped only: fine, it is replayed from the file every launch.
        status['state'] = 'ok'
    elif has_ticket:
        # Chrome drops a portal cookie once it expires, so "no portal cookie"
        # is what a lapsed session looks like on disk. The EU Login ticket is
        # still here, which is precisely the case silent SSO recovers from.
        status['state'] = 'recoverable'
    else:
        status['state'] = 'expired' if status['has_state_file'] else 'ok'

    if status['state'] == 'expired' and has_ticket:
        status['state'] = 'recoverable'
    return status


# ---------------------------------------------------------------------------
# Guard + silent SSO recovery
# ---------------------------------------------------------------------------

def _human_needed(page) -> bool:
    """True when this page will never resolve itself.

    Either EU Login is asking for credentials, or ECAS has turned us away. Both
    mean the wait is pointless; anything else is a redirect still in flight.
    """
    try:
        if page.locator(LOGIN_FORM_SELECTOR).count():
            return True
        title = (page.title() or '').lower()
        return any(marker in title for marker in BLOCKED_PAGE_MARKERS)
    except Exception:  # noqa: BLE001 - a mid-redirect page answers nothing
        return False


def _await_sso(page, timeout_s: float) -> bool:
    """Wait out an EU Login bounce, hoping the ticket re-authenticates us.

    When the EU Login ticket cookie is still valid, ECAS issues a service ticket
    and redirects straight back to the portal without ever rendering a form.
    That is the normal case once the portal's own (much shorter) Drupal session
    lapses, and treating it as fatal — which is what this code used to do — is
    what made a one-time sign-in feel like a weekly chore.

    Returns True if we ended up back on portal content.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if page.is_closed():
                return False
            if not is_login_url(page.url):
                page.wait_for_timeout(1_000)  # let a final redirect land
                return not is_login_url(page.url)
            if _human_needed(page):
                return False  # a credential form, or ECAS turning us away
            page.wait_for_timeout(1_000)
        except Exception:  # noqa: BLE001 - navigation raced us; look again
            time.sleep(1)
    return False


def guard(page, recover: bool = True, timeout_s: float | None = None) -> None:
    """Ensure the page is on portal content, recovering silently if it can.

    Call after every navigation. Raises :class:`SessionExpired` only when the
    session is really gone — a recoverable SSO bounce is waited out instead.
    """
    if not is_login_url(page.url):
        return
    if timeout_s is None:
        timeout_s = settings.ESC_SSO_TIMEOUT
    if recover and _await_sso(page, timeout_s):
        return
    raise SessionExpired(
        'Redirected to EU Login and the saved session could not re-authenticate '
        f'silently — it has expired. {SIGN_IN_HINT}'
    )


def goto_portal(page, url: str, wait_until: str = 'domcontentloaded'):
    """Navigate to a portal URL, surviving an SSO bounce on the way.

    After ECAS hands us back it usually returns to the requested destination,
    but not always — so if we land somewhere else, ask again.
    """
    page.goto(url, wait_until=wait_until)
    if is_login_url(page.url):
        guard(page)
        if urlsplit(page.url).path != urlsplit(url).path:
            page.goto(url, wait_until=wait_until)
            guard(page, recover=False)
    return page


# ---------------------------------------------------------------------------
# The browser profile lock
# ---------------------------------------------------------------------------

_thread_lock = threading.Lock()


def _lock_file_path() -> Path:
    return profile_dir() / 'esc-profile.lock'


def _try_take(fh) -> bool:
    """Take an OS-level lock on the first byte, without blocking.

    An OS lock rather than a pid file because the kernel releases it when the
    holder dies — no stale-lock heuristics, and no ``os.kill(pid, 0)`` liveness
    probe, which on Windows would terminate the process it is probing.
    """
    fh.seek(0)
    if msvcrt is not None:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    if fcntl is not None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    return True  # no primitive available: the in-process lock is all we have


def _release(fh) -> None:
    with contextlib.suppress(Exception):
        fh.seek(0)
        if msvcrt is not None:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(Exception):
        fh.close()


@contextlib.contextmanager
def profile_lock(wait_s: float = 0):
    """Hold exclusive use of the browser profile.

    Raises :class:`ProfileBusy` rather than queueing indefinitely, so the
    heartbeat can simply skip a tick while a run is using the browser.
    """
    got_thread = (
        _thread_lock.acquire(timeout=wait_s) if wait_s > 0
        else _thread_lock.acquire(blocking=False)
    )
    if not got_thread:
        raise ProfileBusy('The browser is already in use by this process.')

    fh = None
    try:
        d = profile_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = _lock_file_path()
        deadline = time.monotonic() + wait_s
        while True:
            fh = open(path, 'a+b')  # noqa: SIM115 - held for the block's lifetime
            if _try_take(fh):
                break
            _release(fh)
            fh = None
            if time.monotonic() >= deadline:
                raise ProfileBusy(
                    'The browser profile is open in another process — a run, a '
                    'login window, or the session heartbeat.'
                )
            time.sleep(0.5)

        with contextlib.suppress(Exception):
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()).encode())
            fh.flush()
        yield
    finally:
        if fh is not None:
            _release(fh)
        _thread_lock.release()


# ---------------------------------------------------------------------------
# Browser contexts
# ---------------------------------------------------------------------------

def _inject_saved_cookies(context) -> None:
    """Replay storage_state.json's cookies into a persistent context.

    Chrome discards session-scoped cookies when a profile closes, and two of
    those (``__Secure-CASTGC``, ``__Secure-ECAS_SESSIONID``) *are* the EU Login
    ticket. Playwright serialises them, so replaying the file on every launch is
    what keeps the sign-in alive across browser restarts.
    """
    cookies = _cookies()
    if not cookies:
        return
    with contextlib.suppress(Exception):
        context.add_cookies(cookies)


def _export(context, page) -> None:
    """Persist refreshed cookies, unless we ended up logged out.

    Exporting from a login page would clobber a good session file with
    logged-out cookies.
    """
    with contextlib.suppress(Exception):
        if page is not None and is_login_url(page.url):
            return
        context.storage_state(path=str(storage_state_path()))


@contextlib.contextmanager
def browser_context(headless: bool | None = None, wait_s: float = 0):
    """Yield a Playwright page carrying the saved portal session.

    Prefers the persistent profile the interactive login created — same browser
    identity, so ECAS's device trust still applies — and falls back to a plain
    Chromium context driven from ``storage_state.json``. Either way the
    refreshed cookies are written back on the way out, so every run pushes the
    session's expiry forward instead of ageing a snapshot.

    Usage::

        with browser_context() as page:
            auth.goto_portal(page, url)
    """
    if headless is None:
        headless = settings.ESC_HEADLESS
    if not has_any_session():
        raise SessionExpired(f'No saved session. {SIGN_IN_HINT}')

    state_path = storage_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    with profile_lock(wait_s=wait_s):
        with sync_playwright() as p:
            browser = None
            page = None
            if has_profile():
                context = _launch_persistent(p, headless=headless)
                _inject_saved_cookies(context)
            else:
                browser = p.chromium.launch(
                    headless=headless, args=list(settings.ESC_BROWSER_ARGS)
                )
                context = browser.new_context(
                    storage_state=str(state_path) if has_session() else None,
                    locale='en-GB',
                    viewport={'width': 1440, 'height': 900},
                )
            try:
                # Portal pages are Drupal-slow under load; 45s beats spurious
                # timeouts that look like logic bugs.
                context.set_default_timeout(45_000)
                pages = context.pages
                page = pages[0] if pages else context.new_page()
                yield page
            finally:
                _export(context, page)
                with contextlib.suppress(Exception):
                    context.close()
                if browser is not None:
                    with contextlib.suppress(Exception):
                        browser.close()


def _wait_until_logged_in(page, timeout_s: int) -> None:
    """Block until the page settles on portal content, or time out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if page.is_closed():
            raise TimeoutError('Browser window was closed before login completed.')
        if not is_login_url(page.url):
            page.wait_for_timeout(1_500)  # let any final redirect land
            if not is_login_url(page.url):
                return
        page.wait_for_timeout(1_000)
    raise TimeoutError(
        f'Login not completed within {timeout_s}s. Nothing was saved.'
    )


def _launch_persistent(p, headless: bool = False, echo=None):
    """Launch the user's installed browser with a persistent profile.

    "Same browser" in practice: real Chrome/Edge (not Playwright's bundled
    Chromium) against a dedicated profile dir that survives between logins, so
    after the first sign-in the session is remembered. Falls back down the
    channel list, then to bundled Chromium. Headed for interactive login,
    headless for later runs against the same profile.
    """
    d = profile_dir()
    d.mkdir(parents=True, exist_ok=True)

    preferred = settings.ESC_BROWSER_CHANNEL
    channels = [preferred] + [c for c in ('chrome', 'msedge') if c != preferred] + [None]
    last_err = None
    for channel in channels:
        try:
            kwargs = dict(
                user_data_dir=str(d), headless=headless,
                locale='en-GB', viewport={'width': 1440, 'height': 900},
                args=list(settings.ESC_BROWSER_ARGS),
            )
            if channel:
                kwargs['channel'] = channel
            ctx = p.chromium.launch_persistent_context(**kwargs)
            if echo:
                echo(f'Opened your {channel or "bundled Chromium"} browser.')
            return ctx
        except Exception as exc:  # noqa: BLE001 - try the next channel
            last_err = exc
    raise RuntimeError(f'Could not launch a browser: {last_err}')


def interactive_login(
    pass_id: str | int, timeout_s: int = 600, echo=print, persistent: bool = True
) -> Path:
    """Open a real browser, wait for the human to log in, save the session.

    ``persistent=True`` — the default, and what both the CLI and the Settings
    button use — signs in inside the very profile later runs reuse, so ECAS
    remembers the device and the sign-in is genuinely one-time. The cookies are
    also exported to ``storage_state.json``, the only carrier for the
    session-scoped EU Login ticket.

    Returns the storage-state path. Raises TimeoutError if login never lands.
    """
    target = search_url(pass_id)
    state_path = storage_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    with profile_lock(wait_s=10):
        with sync_playwright() as p:
            browser = None
            if persistent:
                context = _launch_persistent(p, headless=False, echo=echo)
                _inject_saved_cookies(context)
                page = context.pages[0] if context.pages else context.new_page()
            else:
                browser = p.chromium.launch(
                    headless=False, args=list(settings.ESC_BROWSER_ARGS)
                )
                context = browser.new_context(
                    storage_state=str(state_path) if has_session() else None,
                    locale='en-GB',
                    viewport={'width': 1440, 'height': 900},
                )
                page = context.new_page()
            try:
                context.set_default_timeout(60_000)
                page.goto(target, wait_until='domcontentloaded')

                echo('A browser window has opened — complete EU Login there '
                     '(including any 2FA). Nothing is typed for you.')

                _wait_until_logged_in(page, timeout_s)

                context.storage_state(path=str(state_path))
                echo(f'Signed in. Session saved to {state_path}')
            finally:
                context.close()
                if browser is not None:
                    browser.close()

    return state_path


def verify_session(pass_id: str | int, wait_s: float = 0) -> bool:
    """Headless check that the saved session actually reaches portal content."""
    try:
        with browser_context(headless=True, wait_s=wait_s) as page:
            goto_portal(page, search_url(pass_id))
            return not is_login_url(page.url)
    except (SessionExpired, ProfileBusy):
        return False
