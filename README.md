# ESC Candidate Outreach

Django + Playwright automation for the European Solidarity Corps organisation
admin area on `youth.europa.eu` — the **PASS** "Search and contact" tool. Save a
set of candidate search filters, then contact the matching candidates at a
controlled pace, without ever contacting the same person twice.

## What the search actually is

`/admin/esc/pass/<id>/search_en` is a Drupal **multi-step form**
(`eyp_esc_pass_search_form`, POST), not a URL-driven search. The flow, confirmed
by recon against the live page:

1. **Funding programme** (`fp`; `5` = ESC51 Volunteering) → *Next*.
2. **Search criteria** — required: earliest start date, latest end date,
   duration (months), and Activity topics. Optional: activity country, country
   of residence, Has CV, motivation statement, knowledge/experience, and
   languages (each multi-select has an all / at-least-one toggle).
3. **Search** → results render into a `#edit-results` table with columns
   *Ref, Name, Actions, Contact Status, Offer Status*.

The full filter set (all 65 countries, 10 topics, 18 knowledge areas, 162
languages) is captured in [`esc/portal/filter_schema.json`](esc/portal/filter_schema.json);
the web UI's filter builder renders itself from it. The wizard driver lives in
[`esc/portal/adapter.py`](esc/portal/adapter.py) (`run_search`) and is verified
working end-to-end against the live portal.

The portal enforces consent at source — the criteria panel states results are
limited to participants who **"allowed to be contacted"**.

## Why login is manual

The page is behind EU Login (ECAS):

```
/admin/esc/pass/82413/search_en
  → 302 → /eulogin_en?destination=…
  → 302 → ecas.ec.europa.eu/cas/login?authenticationLevel=MEDIUM
  → non-browser client → sorry.ec.europa.eu   (blocked)
```

ECAS blocks anything that is not a real browser, so there is no scripted login
and **this tool never handles your password**. You sign in by hand once; the
resulting browser session is saved and reused headlessly.

"Once" is meant literally, and three things make it true:

- **The sign-in happens in the profile every later run uses.** A persistent
  Chrome profile holds the browser identity EU Login ties its 2FA device-trust
  to; `storage_state.json` holds the session-scoped cookies (the EU Login
  ticket) that Chrome discards when a profile closes. Neither survives alone,
  so every launch injects the file into the profile and exports it again on the
  way out.
- **A bounce to EU Login is not a failure.** The portal's own Drupal session
  lasts weeks; the EU Login ticket outlives it. When the portal redirects a run
  to EU Login, the ticket normally re-authenticates silently and the run
  carries on. Only an actual username/password form means the session is really
  gone — and then the run ends in `NEEDS_LOGIN` rather than a parse error.
- **The session is kept warm.** The portal runs Drupal's autologout module, so
  an idle session is dropped well before its cookie expiry. While the app is
  running it loads one page every 15 minutes and re-saves the refreshed
  cookies. It skips itself whenever a run, a login window or another process
  has the browser.

Settings shows the real state, read from the cookies themselves — *signed in*,
*expires soon* with the date, or *expired* — rather than "there is a file here".

## What still needs one populated search

Every recon search for placement 82413 returned **"No results"** (no contactable
candidates matched the criteria tried), so a real result *row* was never seen.
Two things therefore remain unconfirmed and are guarded by
`SELECTORS_CONFIRMED = False` in the adapter:

- the internal markup of a result row (the Name link / candidate id, Actions cell);
- the **contact control** and its message form.

Searching works today; reading rows and contacting are blocked until you run a
search that returns people and capture those selectors (see step 3 below).

## Setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python manage.py migrate
```

### 1. Sign in (once, by hand)

```bash
python manage.py esc_login --pass-id 82413
```

A browser opens. Complete EU Login including 2FA. The session is written to
`storage_state.json`.

> `storage_state.json` is a **live admin session for your organisation**.
> It is gitignored. Treat it like a password; delete it to sign out.

### 2. Recon — teach the tool what the page looks like

```bash
python manage.py esc_inspect --pass-id 82413 --capture-action
```

Writes to `recon/`:

| File | What it gives you |
|---|---|
| `page.html`, `screenshot.png` | the page as rendered |
| `forms.json` | every form, field, type and `<option>` list |
| `filter_schema.json` | the search filters, **consumed automatically by the web UI** |
| `repeats.json` | repeated block structures — your `result_row` selector |
| `network.json` | requests fired during search and pagination |
| `action_requests.json` | the request fired when you contact one candidate by hand |

`--capture-action` pauses and asks you to contact **exactly one** candidate
manually. That is a real message to a real person — pick deliberately. The
captured request is what reveals the outreach endpoint.

### 3. Confirm the result-row + contact selectors

This needs a search that returns candidates. In the app, create a profile whose
filters actually match people for your placement and run a **dry run** — if it
reports rows found, the search side is good. Then, to capture the contact
control, run recon with a populated search and contact ONE person by hand:

```bash
python manage.py esc_inspect --pass-id 82413 --capture-action   --filters '{"fp":"5","start_date":"2026-01-01", ...}'
```

With `--filters` the command drives the whole search wizard before dumping, so
`recon/` describes a *populated* result set — which is the thing that has never
been captured. Without it you only get step 1 of the wizard.

Open `esc/portal/adapter.py`, fill the `'TODO'` entries in `SELECTORS`
(`row_contact_status`, `contact_button`, `message_body`, `message_submit`,
`send_confirmation`) from `recon/`, implement `send_outreach`, then set:

```python
SELECTORS_CONFIRMED = True
```

Until you do, live runs are refused at three separate layers and the UI shows a
warning banner. The search filters need no hand-editing — the builder renders
from `esc/portal/filter_schema.json`.

### 4. Run

```bash
python manage.py runserver
```

Open <http://127.0.0.1:8000/>. Create a message template, create a search
profile, then **Dry run** before **Run live**.

From the command line:

```bash
python manage.py esc_run --profile "Romania 2026"          # dry run
python manage.py esc_run --profile "Romania 2026" --live   # contacts people
```

## Safeguards

These are load-bearing, not decoration:

- **No double contact, guaranteed by the database.** A unique constraint on
  `(search_profile, candidate)` means a crash mid-run cannot cause a repeat.
  A `SENT` row is terminal. A `DRY_RUN` row is upgraded in place, so previewing
  never blocklists anyone.
- **The row is claimed before the portal is asked to send.** The runner writes
  a `SENDING` row first and upgrades it to `SENT` only once the portal has
  answered. So a crash, a kill switch or a server reload mid-send leaves one
  send *recorded that may not have happened* — never one sent twice. A
  `SENDING` row blocks that person from further contact and is flagged in the
  UI until you check the portal and say which it was; "was not sent" releases
  them for a later run.
- **One run at a time.** Two runs would each read the already-contacted set
  before either wrote to it, and the unique constraint would stop the second
  *row* but not the second *message*. A run left behind by a killed server is
  failed automatically at the next start, so the slot cannot stay wedged.
- **Dry run is the default** everywhere: the UI, the CLI, and the model default.
- **Caps**: per run, per day, plus a global hard ceiling a profile cannot exceed.
- **Kill switch** in Settings, checked between every candidate.
- **Randomised delay** (default 8–20s) between contacts.
- **Session expiry is a state, not a crash** — a run first tries to
  re-authenticate silently through EU Login, and only if that fails ends in
  `NEEDS_LOGIN` with instructions rather than a confusing parse error.
- **Minimal personal data.** Only the portal's opaque candidate id, a display
  name for readable logs, and the profile link are stored. As an organisation
  you are a GDPR controller; a local mirror of candidate profiles would be a
  liability with no functional payoff. `recon/` is gitignored for the same
  reason.

## Things to confirm before running this at scale

- Whether the portal's Terms of Use permit automated interaction with the admin
  area. `robots.txt` disallows `/admin/` — that governs crawlers rather than a
  logged-in admin driving their own account, but it signals intent.
- Whether the portal imposes its own contact quota per placement. If it does,
  set `per_day_cap` below it.
- Bulk unsolicited contact is the fastest way to get an organisation account
  suspended. The defaults here are deliberately slow.

## Layout

```
config/settings.py         portal URLs, caps, paths
esc/models.py              SearchProfile, Candidate, Outreach, Run, templates
esc/runner.py              the run executor (background thread)
esc/portal/auth.py         EU Login session capture, reuse and SSO recovery
esc/portal/heartbeat.py    keeps the saved session warm while the app runs
esc/portal/adapter.py      ← the ONLY file holding portal selectors
esc/views.py, urls.py      web UI
esc/management/commands/   esc_login, esc_inspect, esc_run
esc/tests.py               69 tests, mostly on the safety guarantees
```

If the portal is redesigned, `esc/portal/adapter.py` is the file to fix.

## Tests

```bash
python manage.py test esc
```
