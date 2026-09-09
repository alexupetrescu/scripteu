"""The single place that knows the shape of the ESC portal.

What the portal actually is (confirmed by recon against the live page):

The search at ``/admin/esc/pass/<id>/search_en`` is the **PASS** "Search and
contact" tool — a Drupal multi-step form (``eyp_esc_pass_search_form``), POST,
not a URL-driven search. The flow is:

  1. Pick the **Funding programme** (``fp``; ``5`` = ESC51 Volunteering) → Next.
  2. Fill the search criteria. Three fields are **required**: earliest start
     date, latest end date, duration (months), plus **Activity topics**.
  3. Click **Search** (``name=submit``). Results render into a ``#edit-results``
     table with columns: Ref, Name, Actions, Contact Status, Offer Status.

Because the field ``id`` attributes carry random per-render suffixes
(``edit-fp--Sl5I0rWOF3I``), every selector here keys off ``name=``.

STILL UNCONFIRMED — needs one *populated* result set:
the internal markup of a result row (the Name link / candidate id, the Actions
cell) and the contact control. Every recon search returned "No results" for
this placement, so no real row was ever seen. Those selectors are marked below
and guarded by :data:`SELECTORS_CONFIRMED`; searching works before then, only
reading rows and contacting is blocked.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.conf import settings

from . import auth


class AdapterNotConfigured(RuntimeError):
    """A row/contact selector is still unconfirmed. Carries the fix."""


# ---------------------------------------------------------------------------
# Selectors  (name-based: ids have random suffixes)
# ---------------------------------------------------------------------------

# CONFIRMED against the live page:
WIZARD = {
    'form': 'form.eyp-esc-pass-search-form',
    'funding_programme': 'select[name="fp"]',
    'next': 'input[name="next"]',        # step 1 → criteria
    'submit': 'input[name="submit"]',    # criteria → results ("🔍 Search")
    'back': 'input[name="back"]',
    'results_table': '#edit-results',
    'result_rows': '#edit-results > tbody > tr',
    'empty_marker': '#edit-results td.empty.message',   # "No results"
    'results_details': '#edit-search-result',           # the <details> wrapper
}

# The funding programme value seen in recon (ESC51 Volunteering).
DEFAULT_FUNDING_PROGRAMME = '5'

# NOT yet confirmed — fill from a populated search, then flip the flag below.
SELECTORS_CONFIRMED = False
SELECTORS = {
    # Within a result row (a <tr> under #edit-results):
    'row_ref': 'td:nth-child(1)',        # "Ref" column — likely the candidate ref
    'row_name': 'td:nth-child(2)',       # "Name" column (link to the candidate?)
    'row_name_link': 'td:nth-child(2) a',
    'row_actions': 'td:nth-child(3)',    # "Actions" (colspan 3 in the header)
    'row_contact_status': 'TODO',        # "Contact Status" column
    # The contact control + message form (revealed by --capture-action):
    'contact_button': 'TODO',
    'message_body': 'TODO',
    'message_submit': 'TODO',
    'send_confirmation': 'TODO',
}

# Pull the candidate ref/id out of a row link. Refined once a real row is seen.
CANDIDATE_ID_RE = re.compile(r'/(?:participant|candidate|user|profile|youth)/(\w+)')
# Pager caption format: "1 - 50 / 137"
PAGER_RE = re.compile(r'(\d+)\s*-\s*(\d+)\s*/\s*(\d+)')


def unconfigured_keys() -> list[str]:
    return [k for k, v in SELECTORS.items() if not v or v == 'TODO']


def is_configured() -> bool:
    return SELECTORS_CONFIRMED and not unconfigured_keys()


def ensure_configured() -> None:
    if is_configured():
        return
    missing = unconfigured_keys() or ['(SELECTORS_CONFIRMED is still False)']
    raise AdapterNotConfigured(
        'The result-row and contact selectors are not confirmed yet, so this '
        'app will not read candidate rows or contact anyone.\n\n'
        f'Unconfirmed: {", ".join(missing)}\n\n'
        'These need one search that actually returns candidates:\n'
        '  1. python manage.py esc_login   --pass-id 82413\n'
        '  2. python manage.py esc_inspect --pass-id 82413 --capture-action\n'
        '     (fill in criteria that match people, then contact ONE by hand)\n'
        '  3. Fill in SELECTORS in esc/portal/adapter.py from recon/\n'
        '  4. Set SELECTORS_CONFIRMED = True'
    )


# ---------------------------------------------------------------------------
# Filter schema (bundled, generated from recon)
# ---------------------------------------------------------------------------

_BUNDLED_SCHEMA = Path(__file__).with_name('filter_schema.json')


def _load_schema_file() -> dict:
    # The bundled schema is authoritative: it was generated from a full walk of
    # the wizard. A plain `esc_inspect` only sees step 1 (the funding
    # programme), so its recon/filter_schema.json must NOT shadow the bundled
    # one. The recon file is used only when nothing is bundled.
    for path in (_BUNDLED_SCHEMA, settings.ESC_RECON_DIR / 'filter_schema.json'):
        if path.exists():
            try:
                return json.loads(path.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError):
                continue
    return {}


def load_filter_schema() -> list[dict[str, Any]]:
    """The search-criteria fields, for the UI's filter builder."""
    data = _load_schema_file()
    if isinstance(data, dict):
        return data.get('fields', [])
    return data or []


def funding_programme_field() -> dict:
    data = _load_schema_file()
    return data.get('funding_programme', {}) if isinstance(data, dict) else {}


def schema_is_available() -> bool:
    return bool(load_filter_schema())


# ---------------------------------------------------------------------------
# Data carried between the adapter and the runner
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CandidateRef:
    external_id: str
    display_name: str = ''
    profile_url: str = ''


@dataclass
class OutreachResult:
    ok: bool
    detail: str = ''
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Search — drives the multi-step wizard
# ---------------------------------------------------------------------------

# Filter keys that are not plain form fields (handled specially / ignored).
_CONJUNCTIONS = {
    'projects[]': 'projects_conjunction',
    'knowledges[]': 'knowledges_conjunction',
    'projects_languages[]': 'projects_languages_conjunction',
}


def dismiss_overlays(page) -> None:
    """Close the cookie banner and insurance notice if present."""
    for sel in (
        'button:has-text("Accept only essential cookies")',
        'button:has-text("Accept all cookies")',
        'a:has-text("Dismiss")',
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=3000)
                page.wait_for_timeout(400)
        except Exception:
            pass


def run_search(page, pass_id, filters: dict) -> None:
    """Drive the wizard from a fresh page load to a rendered result set.

    Leaves ``page`` on the results view; call :func:`parse_results` next.
    ``filters`` is a flat {name: value} dict matching the schema field names.
    """
    # goto_portal, not goto+guard: if the portal bounces us to EU Login and the
    # ticket is still good, this rides the redirect back instead of failing.
    auth.goto_portal(page, auth.search_url(pass_id), wait_until='networkidle')
    page.wait_for_timeout(1000)
    dismiss_overlays(page)

    # Step 1: funding programme → Next
    fp = filters.get('fp') or DEFAULT_FUNDING_PROGRAMME
    page.locator(WIZARD['funding_programme']).first.select_option(str(fp))
    page.wait_for_timeout(500)
    page.locator(WIZARD['next']).first.click()
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(800)
    dismiss_overlays(page)

    # Step 2: fill criteria
    _fill_criteria(page, filters)

    # Submit
    page.locator(WIZARD['submit']).first.click()
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(1200)
    auth.guard(page)
    _expand_results(page)


def _fill_criteria(page, filters: dict) -> None:
    for name, value in filters.items():
        if name in ('fp',) or name.endswith('_conjunction'):
            continue
        if value in (None, '', [], False):
            continue
        loc = page.locator(f'[name="{name}"]').first
        if not loc.count():
            continue
        tag = loc.evaluate('el => el.tagName.toLowerCase()')
        input_type = (loc.evaluate('el => el.type || ""') or '').lower()

        if tag == 'select':
            page.locator(f'[name="{name}"]').first.select_option(str(value))
        elif input_type == 'checkbox':
            if value:
                loc.check()
        else:
            loc.fill(str(value))

        # Apply the matching conjunction radio if the caller set one.
        conj_field = _CONJUNCTIONS.get(name)
        if conj_field and filters.get(conj_field) in ('and', 'or'):
            page.locator(
                f'input[name="{conj_field}"][value="{filters[conj_field]}"]'
            ).first.check()


def _expand_results(page) -> None:
    """The Results section is a collapsed <details>; open it before reading."""
    try:
        details = page.locator(WIZARD['results_details']).first
        if details.count():
            summary = details.locator('summary').first
            expanded = details.get_attribute('open')
            if summary.count() and expanded is None:
                summary.click()
                page.wait_for_timeout(300)
    except Exception:
        pass


def result_total(page) -> int | None:
    """Total match count from the pager caption ("1 - 50 / 137" → 137)."""
    try:
        text = page.locator(WIZARD['results_table']).first.inner_text(timeout=3000)
    except Exception:
        return None
    m = PAGER_RE.search(text)
    return int(m.group(3)) if m else None


def extract_candidate_id(href: str) -> str:
    match = CANDIDATE_ID_RE.search(href or '')
    if match:
        return match.group(1)
    digits = re.findall(r'(\d{3,})', href or '')
    if digits:
        return digits[-1]
    return (href or '').strip('/') or 'unknown'


def parse_results(page) -> list[CandidateRef]:
    """Read the visible result rows.

    Returns [] on the confirmed "No results" marker. Requires the row
    selectors to be confirmed (see module docstring).
    """
    auth.guard(page)
    if page.locator(WIZARD['empty_marker']).count():
        return []

    ensure_configured()
    refs: list[CandidateRef] = []
    rows = page.locator(WIZARD['result_rows'])
    for i in range(rows.count()):
        row = rows.nth(i)
        # Skip the empty-message row defensively.
        if row.locator('td.empty.message').count():
            continue
        link = row.locator(SELECTORS['row_name_link']).first
        href = link.get_attribute('href') if link.count() else ''
        ref_cell = row.locator(SELECTORS['row_ref']).first
        name_cell = row.locator(SELECTORS['row_name']).first
        ref_text = ref_cell.inner_text().strip() if ref_cell.count() else ''
        name = name_cell.inner_text().strip() if name_cell.count() else ''
        external_id = extract_candidate_id(href) if href else (ref_text or 'unknown')
        refs.append(CandidateRef(external_id=external_id, display_name=name,
                                 profile_url=href or ''))
    return refs


# ---------------------------------------------------------------------------
# Outreach  (unconfirmed until a real row + capture is available)
# ---------------------------------------------------------------------------

def send_outreach(page, ref: CandidateRef, subject: str, body: str) -> OutreachResult:
    ensure_configured()
    auth.guard(page)
    raise AdapterNotConfigured(
        'send_outreach is not implemented: the contact control has not been '
        'observed yet (every recon search returned no candidates). Capture it '
        'with esc_inspect --capture-action against a populated search, then '
        'implement this using SELECTORS["contact_button"] etc.'
    )


def render_template(text: str, ref: CandidateRef, extra: dict | None = None) -> str:
    values = {
        'name': ref.display_name or 'there',
        'first_name': (ref.display_name or 'there').split(' ')[0],
        'id': ref.external_id,
        **(extra or {}),
    }
    for key, value in values.items():
        text = text.replace('{' + key + '}', str(value))
    return text
