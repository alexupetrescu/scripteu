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

# adduser applies DIR_MODE from /etc/adduser.conf, which is 0750 on current
# Debian and Ubuntu, so this comes out drwxr-x--- scripteu:scripteu. Two other
# accounts must pass through it -- your own, and nginx's -- so open the top
# level and lock the parts that matter instead. Nothing secret sits directly
# in /srv/scripteu.
sudo chmod 0755 /srv/scripteu

sudo -u scripteu git clone https://github.com/alexupetrescu/scripteu.git /srv/scripteu/app
sudo -u scripteu python3 -m venv /srv/scripteu/.venv
sudo -u scripteu /srv/scripteu/.venv/bin/pip install -r /srv/scripteu/app/requirements-vps.txt
```

State that must survive a redeploy, and must never be web-served:

```bash
sudo -u scripteu mkdir -p /srv/scripteu/state /srv/scripteu/staticfiles /srv/scripteu/.playwright

# The portal session and the database live here. 0700: not nginx's business,
# and not any other account's on this box.
sudo chmod 0700 /srv/scripteu/state
```

Everything from here runs **as `scripteu`**, not as you. The checkout is owned
by that account and git refuses to operate on a repository owned by someone
else ("dubious ownership"). `sudo -u scripteu` works despite the `nologin`
shell, because it executes the binary rather than a login shell.

Each `manage.py` call also needs the environment file loaded, so define this
shorthand once per session and use it below:

```bash
esc() { sudo -u scripteu bash -c 'set -a; . /etc/scripteu.env; set +a
        cd /srv/scripteu/app
        exec /srv/scripteu/.venv/bin/python manage.py "$@"' -- "$@"; }
```

## 2. Playwright's browser

Needs system libraries, which is the one step that wants root:

```bash
sudo /srv/scripteu/.venv/bin/playwright install-deps chromium
# "env", not a bare VAR=value: sudo parses that itself and can drop it, which
# silently installs the browser into ~/.cache instead of where the app looks.
sudo -u scripteu env PLAYWRIGHT_BROWSERS_PATH=/srv/scripteu/.playwright \
     /srv/scripteu/.venv/bin/playwright install chromium

ls /srv/scripteu/.playwright        # must list a chromium-* directory
```

## 3. Environment

```bash
sudo cp /srv/scripteu/app/deploy/scripteu.env.example /etc/scripteu.env

# A URL-safe key, deliberately. This file is both sourced by a shell and read
# by systemd, and Django's own get_random_secret_key() emits ) & # $ ^ * --
# put one of those in unquoted and the shell dies on it.
sudo -u scripteu /srv/scripteu/.venv/bin/python -c \
  "import secrets; print(secrets.token_urlsafe(64))"

sudo nano /etc/scripteu.env          # paste the key, check the paths
sudo chown root:scripteu /etc/scripteu.env && sudo chmod 640 /etc/scripteu.env
```

The app refuses to start with `ESC_DEBUG=0` and no key, rather than signing
cookies with a value that is in a public repo.

Check the file parses before moving on. A stray metacharacter aborts the
sourcing at that line, leaving everything after it unset — which surfaces later
as Django complaining about a variable the file plainly contains:

```bash
sudo -u scripteu bash -c 'set -a; . /etc/scripteu.env; set +a
    echo "key length: ${#ESC_SECRET_KEY}, db: $ESC_DB_PATH"'
```

## 4. Database, static files, your login

```bash
esc migrate
esc collectstatic --noinput
esc createsuperuser        # the app's own login, not your EU Login
```

The database is created at `ESC_DB_PATH`, inside `state/` rather than in the
checkout: the outreach history is personal data, and a deploy stays a plain
`git pull`.

## 5. gunicorn

```bash
sudo cp /srv/scripteu/app/deploy/scripteu.service /etc/systemd/system/scripteu.service
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
sudo cp /srv/scripteu/app/deploy/nginx-scripteu.conf /etc/nginx/snippets/scripteu.conf
sudo htpasswd -c /etc/nginx/scripteu.htpasswd <your-name>
sudo chown root:www-data /etc/nginx/scripteu.htpasswd
sudo chmod 640 /etc/nginx/scripteu.htpasswd
```

Add one line inside the existing `server { }` block for avestudio.ro (the HTTPS
one), then reload:

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

## 7. The login console

The EU Login sign-in needs a real browser, and it has to be a browser *on this
server* -- that is where the session cookies must end up. So the server runs
one on a virtual screen and shows it to you inside the app, over noVNC. You do
the sign-in from any machine, in your own browser, at
`avestudio.ro/scripteu/settings/`. No VNC client, no terminal.

```bash
sudo apt install -y xvfb x11vnc novnc websockify

# 6081 must be free -- this host already runs things on 3000 and 8000.
ss -ltnp | grep 6081 || echo "6081 free"

sudo cp /srv/scripteu/app/deploy/scripteu-xvfb.service   /etc/systemd/system/
sudo cp /srv/scripteu/app/deploy/scripteu-x11vnc.service /etc/systemd/system/
sudo cp /srv/scripteu/app/deploy/scripteu-novnc.service  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now scripteu-xvfb scripteu-x11vnc scripteu-novnc
systemctl is-active scripteu-xvfb scripteu-x11vnc scripteu-novnc
```

Add `DISPLAY=:99`, `ESC_VNC_ENABLED=1` and `ESC_VNC_URL` to `/etc/scripteu.env`
(they are in the template), refresh the nginx snippet for the `/scripteu/vnc/`
location, and restart:

```bash
sudo cp /srv/scripteu/app/deploy/nginx-scripteu.conf /etc/nginx/snippets/scripteu.conf
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl restart scripteu
```

Then, in your browser: **Settings -> Open login in same browser**. The server's
browser appears in the page. Complete EU Login there, including 2FA. When it
finishes, the app verifies the session headlessly and the panel flips to
**signed in**.

Run a **dry run** before anything else.

Two things to know about this console:

- **It is guarded by the basic-auth password only.** It is not a Django view,
  so the app's own login does not cover it, and whoever opens it is driving a
  real browser on your server. Keep that htpasswd tight.
- **`PrivateTmp` must stay off** in `scripteu.service`. X's socket lives in
  `/tmp/.X11-unix`, and a private `/tmp` means the login browser cannot find
  the screen -- you would get a blank console and a timeout.

## When you have to sign in again

Rarely, by design. The portal's own session lapses in weeks and the app rides
the EU Login redirect back without asking; the heartbeat keeps it warm meanwhile.
You only repeat step 7 when Settings says **expired** — i.e. EU Login itself has
let the ticket go.

## Updating

```bash
sudo -u scripteu git -C /srv/scripteu/app pull
sudo -u scripteu /srv/scripteu/.venv/bin/pip install -r /srv/scripteu/app/requirements-vps.txt
esc migrate
esc collectstatic --noinput
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
| `cd`: Permission denied as your own user | `/srv/scripteu` is `0750` from `adduser` — `sudo chmod 0755 /srv/scripteu` |
| 403 on `/scripteu/static/...` | same cause: nginx cannot traverse a `0750` home either |
| git "dubious ownership" | run git as the owner: `sudo -u scripteu git -C /srv/scripteu/app ...` |
| gunicorn exits `status=3` | worker failed to boot — `journalctl -u scripteu -n 50`; usually `WorkingDirectory` not pointing at the checkout |
| 502, permission denied | socket ownership: unit `Group=` vs nginx's user |
| 404 on every page | the `:/` at the end of `proxy_pass`, and `ESC_SCRIPT_NAME` — you need both |
| CSS missing, admin unstyled | `collectstatic`, and the `alias` path in the snippet |
| CSRF verification failed | `ESC_CSRF_TRUSTED_ORIGINS` must include the `https://` scheme |
| Logged out of another app on the domain | should not happen — cookies here are `scripteu_*` and scoped to the prefix |
| Browser fails to start | `ESC_BROWSER_ARGS=--no-sandbox,--disable-dev-shm-usage`, and `playwright install-deps` |
| Settings says expired right after signing in | ECAS may have rejected the session; check `journalctl -u scripteu` |
| `No matching distribution found for Django` | the interpreter is too old for the pinned Django; check `python3 -V` (5.2 LTS needs 3.10+) |
| `/etc/scripteu.env: syntax error near unexpected token` | a value contains shell metacharacters — regenerate the key with `secrets.token_urlsafe`, or single-quote the value |
| `ESC_SECRET_KEY must be set` when it *is* in the file | same cause: sourcing aborted on an earlier line, so nothing after it was exported |

```bash
journalctl -u scripteu -f
sudo tail -f /var/log/nginx/error.log
```
