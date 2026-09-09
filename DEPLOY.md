# Deploying to a VPS, under `avestudio.ro/scripteu`

This host already runs several Django apps behind gunicorn, so nothing here
takes a port, a global nginx setting, or a shared cookie name. The app binds a
unix socket, mounts under a path prefix, and namespaces its own cookies.

Two things about this particular app shape the whole setup:

- **It holds a live admin credential.** The saved portal session can act as your
  organisation, and the UI has a button that messages real people. It is
  therefore behind HTTP basic auth *and* a Django login, and the session files
  live outside any web-served directory.
- **It needs a real browser.** EU Login cannot be scripted, so the one-time
  sign-in happens in a browser running *on the server*, reached over VNC through
  an SSH tunnel. The session is then minted from the VPS's own IP, which is the
  IP every later run will come from.

Placeholders below: `/srv/scripteu` (deploy path), `scripteu` (service user),
`www-data` (nginx's group). Change them to match the other apps on the host.

---

## 1. User, code, virtualenv

```bash
sudo adduser --system --group --home /srv/scripteu --shell /usr/sbin/nologin scripteu
sudo -u scripteu git clone https://github.com/alexupetrescu/scripteu.git /srv/scripteu/app
cd /srv/scripteu/app

sudo -u scripteu python3 -m venv /srv/scripteu/.venv
sudo -u scripteu /srv/scripteu/.venv/bin/pip install -r requirements-vps.txt
```

State that must survive a redeploy, and must never be web-served:

```bash
sudo -u scripteu mkdir -p /srv/scripteu/state /srv/scripteu/staticfiles /srv/scripteu/.playwright
sudo chmod 700 /srv/scripteu/state
```

## 2. Playwright's browser

Needs system libraries, which is the one step that wants root:

```bash
sudo /srv/scripteu/.venv/bin/playwright install-deps chromium
sudo -u scripteu PLAYWRIGHT_BROWSERS_PATH=/srv/scripteu/.playwright \
     /srv/scripteu/.venv/bin/playwright install chromium
```

## 3. Environment

```bash
sudo cp deploy/scripteu.env.example /etc/scripteu.env
sudo /srv/scripteu/.venv/bin/python -c \
  "from django.core.management.utils import get_random_secret_key as k; print(k())"
sudo nano /etc/scripteu.env          # paste the key, check the paths
sudo chown root:scripteu /etc/scripteu.env && sudo chmod 640 /etc/scripteu.env
```

The app refuses to start with `ESC_DEBUG=0` and no key, rather than signing
cookies with a value that is in a public repo.

## 4. Database, static files, your login

```bash
cd /srv/scripteu/app
sudo -u scripteu bash -c 'set -a; . /etc/scripteu.env; set +a; \
  /srv/scripteu/.venv/bin/python manage.py migrate && \
  /srv/scripteu/.venv/bin/python manage.py collectstatic --noinput && \
  /srv/scripteu/.venv/bin/python manage.py createsuperuser'
```

## 5. gunicorn

```bash
sudo cp deploy/scripteu.service /etc/systemd/system/scripteu.service
sudo nano /etc/systemd/system/scripteu.service      # user, group, paths
sudo systemctl daemon-reload
sudo systemctl enable --now scripteu
systemctl status scripteu --no-pager
```

**One worker, on purpose.** A run executes on a background thread inside the
worker; the heartbeat, the browser-profile lock and the one-run-at-a-time slot
all assume a single process. `--max-requests` is absent because recycling the
worker mid-run would kill it.

## 6. nginx

```bash
sudo cp deploy/nginx-scripteu.conf /etc/nginx/snippets/scripteu.conf
sudo htpasswd -c /etc/nginx/scripteu.htpasswd <your-name>
sudo chown root:www-data /etc/nginx/scripteu.htpasswd && sudo chmod 640 /etc/nginx/scripteu.htpasswd
```

Add one line inside the existing `server { }` block for avestudio.ro (the HTTPS
one), then:

```nginx
include /etc/nginx/snippets/scripteu.conf;
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

nginx's user must be able to reach the socket — that is what `Group=www-data`
plus `--umask 007` in the unit are for. If you get `502` with *permission
denied* in the error log, that pairing is wrong.

`https://avestudio.ro/scripteu/` should now ask for basic auth, then show the
app's own sign-in page.

## 7. The EU Login sign-in, once

The only step that needs a real browser. Nothing is typed for you and no
password passes through this tool.

```bash
sudo apt install -y xvfb x11vnc

# Stop the service first: it would otherwise be holding the browser profile.
sudo systemctl stop scripteu

sudo -u scripteu Xvfb :99 -screen 0 1440x900x24 &
sudo -u scripteu x11vnc -display :99 -localhost -rfbport 5901 -nopw -forever &
```

`-localhost` means the VNC port is not exposed to the internet. Reach it from
your own machine through the SSH tunnel:

```bash
ssh -L 5901:localhost:5901 <you>@avestudio.ro       # leave this open
```

Point any VNC viewer at `localhost:5901`, then, back on the server:

```bash
cd /srv/scripteu/app
sudo -u scripteu bash -c 'set -a; . /etc/scripteu.env; set +a; \
  DISPLAY=:99 ESC_HEADLESS=0 /srv/scripteu/.venv/bin/python manage.py esc_login --pass-id 82413'
```

A browser appears in your VNC window. Complete EU Login including 2FA. The
session is saved to `/srv/scripteu/state/`, verified headlessly, and reused from
then on.

```bash
sudo pkill -u scripteu x11vnc; sudo pkill -u scripteu Xvfb
sudo systemctl start scripteu
```

Open Settings in the app: it should read **signed in**, with the two expiry
dates and the keep-alive line. Then run a **dry run** before anything else.

## When you have to sign in again

Rarely, by design. The portal's own session lapses in weeks and the app rides
the EU Login redirect back without asking; the heartbeat keeps it warm meanwhile.
You only repeat step 7 when Settings says **expired** — i.e. EU Login itself has
let the ticket go.

## Updating

```bash
cd /srv/scripteu/app
sudo -u scripteu git pull
sudo -u scripteu /srv/scripteu/.venv/bin/pip install -r requirements-vps.txt
sudo -u scripteu bash -c 'set -a; . /etc/scripteu.env; set +a; \
  /srv/scripteu/.venv/bin/python manage.py migrate && \
  /srv/scripteu/.venv/bin/python manage.py collectstatic --noinput'
sudo systemctl restart scripteu
```

A restart kills any run in flight. The app fails those rows on the next start
instead of leaving them stuck, and flags any unconfirmed send in the outreach
log for you to settle against the portal — so restart between runs, not during.

## The two warnings you are meant to see

`manage.py check --deploy` reports `security.W004` (no HSTS) and
`security.W008` (no `SECURE_SSL_REDIRECT`). Both are deliberate: this app is
mounted at a path on a domain it shares with other sites, so an HSTS header it
emitted would apply to *all* of avestudio.ro, and a Django-level HTTPS redirect
would fight the proxy that already does it. Set both in nginx, for the domain,
where they belong. Everything else should come back clean.

## If something is wrong

| Symptom | Look at |
|---|---|
| 502, permission denied | socket ownership: unit `Group=` vs nginx's user |
| 404 on every page | the `:/` at the end of `proxy_pass`, and `ESC_SCRIPT_NAME` — you need both |
| CSS missing, admin unstyled | `collectstatic`, and the `alias` path in the snippet |
| CSRF verification failed | `ESC_CSRF_TRUSTED_ORIGINS` must include the `https://` scheme |
| Logged out of another app on the domain | should not happen — cookies here are `scripteu_*` and scoped to the prefix |
| Browser fails to start | `ESC_BROWSER_ARGS=--no-sandbox,--disable-dev-shm-usage`, and `playwright install-deps` |
| Settings says expired right after signing in | ECAS may have rejected the session; check `journalctl -u scripteu` |

```bash
journalctl -u scripteu -f
sudo tail -f /var/log/nginx/error.log
```
