"""Discover the structure of the ESC search page.

This is the command that replaces guesswork. It reuses the saved EU Login
session, opens the real search page, and dumps everything needed to write the
selectors in ``esc/portal/adapter.py``:

  page.html            full rendered markup
  forms.json           every form, field, type and <option> list
  filter_schema.json   the search filters, in the shape the web UI renders
  repeats.json         repeated block structures — candidate result rows
  network.json         requests fired during search / pagination / action
  screenshot.png       what the page actually looked like

With ``--capture-action`` it also waits while you click "contact" on ONE
candidate by hand, recording the exact request that produces. That single
capture is what reveals the outreach endpoint and payload.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from esc.portal import adapter, auth
from esc.portal.auth import SessionExpired

# Drupal plumbing that is never a user-facing filter.
BORING_FIELDS = {
    'form_build_id', 'form_token', 'form_id', 'honeypot_time', '',
}
BORING_TYPES = {'hidden', 'submit', 'button', 'image', 'reset', 'file'}

EXTRACT_FORMS_JS = """
() => {
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return l.textContent.trim();
    }
    const wrap = el.closest('label');
    if (wrap) return wrap.textContent.trim();
    const near = el.closest('.form-item, .js-form-item, .form-group');
    const l2 = near && near.querySelector('label');
    return l2 ? l2.textContent.trim() : '';
  };
  return Array.from(document.querySelectorAll('form')).map((f) => ({
    id: f.id || null,
    name: f.getAttribute('name'),
    action: f.action,
    method: (f.getAttribute('method') || 'get').toLowerCase(),
    classes: f.className,
    fields: Array.from(f.querySelectorAll('input, select, textarea')).map((el) => ({
      tag: el.tagName.toLowerCase(),
      type: (el.type || '').toLowerCase(),
      name: el.name || null,
      id: el.id || null,
      label: labelFor(el),
      placeholder: el.getAttribute('placeholder') || '',
      value: el.type === 'password' ? null : (el.value || ''),
      options: el.tagName === 'SELECT'
        ? Array.from(el.options).map((o) => ({ value: o.value, label: o.textContent.trim() }))
        : null,
    })),
  }));
}
"""

# Find repeated sibling structures: the shape a result list always has.
FIND_REPEATS_JS = """
() => {
  const groups = new Map();
  document.querySelectorAll('*').forEach((el) => {
    const cls = typeof el.className === 'string' ? el.className.trim() : '';
    if (!cls) return;
    const sig = el.tagName.toLowerCase() + '.' + cls.split(/\\s+/).join('.');
    if (!groups.has(sig)) groups.set(sig, []);
    groups.get(sig).push(el);
  });
  const out = [];
  for (const [selector, els] of groups) {
    if (els.length < 3) continue;
    if (!els[0].querySelector('a')) continue;
    out.push({
      selector,
      count: els.length,
      links: Array.from(els[0].querySelectorAll('a')).slice(0, 5)
        .map((a) => ({ href: a.getAttribute('href'), text: a.textContent.trim().slice(0, 80) })),
      sample_html: els[0].outerHTML.slice(0, 3000),
    });
  }
  return out.sort((a, b) => b.count - a.count).slice(0, 10);
}
"""

IGNORED_RESOURCES = {'image', 'stylesheet', 'font', 'media'}


class Command(BaseCommand):
    help = 'Dump the structure of the ESC search page into recon/.'

    def add_arguments(self, parser):
        parser.add_argument('--pass-id', required=True)
        parser.add_argument(
            '--filters', default='{}',
            help='Optional JSON of filters to apply, e.g. \'{"country":"RO"}\'.',
        )
        parser.add_argument(
            '--capture-action', action='store_true',
            help='Stay open (headed) while you click "contact" on ONE candidate, '
                 'recording the request it fires.',
        )
        parser.add_argument(
            '--headed', action='store_true',
            help='Show the browser during the dump.',
        )

    def handle(self, *args, **options):
        try:
            filters = json.loads(options['filters'])
        except json.JSONDecodeError as exc:
            raise CommandError(f'--filters is not valid JSON: {exc}') from exc

        out_dir = settings.ESC_RECON_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        headed = options['headed'] or options['capture_action']
        requests: list[dict] = []

        try:
            # Wait rather than fail if the session heartbeat holds the browser.
            with auth.browser_context(headless=not headed, wait_s=90) as page:
                page.on('request', lambda r: self._record(requests, r))

                if filters:
                    # Drive the wizard, so what gets dumped is a *populated*
                    # result set — the one thing recon has never captured, and
                    # the only way to confirm the row and contact selectors.
                    self.stdout.write(
                        f'Running the search wizard for placement {options["pass_id"]}'
                    )
                    adapter.run_search(page, options['pass_id'], filters)
                else:
                    url = auth.search_url(options['pass_id'])
                    self.stdout.write(f'Opening {url}')
                    auth.goto_portal(page, url)
                page.wait_for_timeout(2500)

                self._dump_page(page, out_dir)
                forms = self._dump_forms(page, out_dir)
                self._dump_schema(forms, out_dir)
                self._dump_repeats(page, out_dir)

                if options['capture_action']:
                    self._capture_action(page, requests, out_dir)

                self._write(out_dir / 'network.json', {'requests': requests})
        except SessionExpired as exc:
            raise CommandError(
                f'{exc}\n\nRun:  python manage.py esc_login '
                f'--pass-id {options["pass_id"]}'
            ) from exc

        self.stdout.write(self.style.SUCCESS(f'\nRecon written to {out_dir}'))
        self.stdout.write(
            'Review forms.json and repeats.json, then fill in SELECTORS in '
            'esc/portal/adapter.py and set SELECTORS_CONFIRMED = True.'
        )

    # -- steps ------------------------------------------------------------

    def _record(self, sink: list, request) -> None:
        if request.resource_type in IGNORED_RESOURCES:
            return
        sink.append({
            'method': request.method,
            'url': request.url,
            'resource_type': request.resource_type,
            'post_data': (request.post_data or '')[:4000],
        })

    def _dump_page(self, page, out_dir) -> None:
        (out_dir / 'page.html').write_text(page.content(), encoding='utf-8')
        page.screenshot(path=str(out_dir / 'screenshot.png'), full_page=True)
        self.stdout.write('  page.html, screenshot.png')

    def _dump_forms(self, page, out_dir) -> list[dict]:
        forms = page.evaluate(EXTRACT_FORMS_JS)
        self._write(out_dir / 'forms.json', {'url': page.url, 'forms': forms})
        self.stdout.write(f'  forms.json ({len(forms)} form(s))')
        return forms

    def _dump_schema(self, forms: list[dict], out_dir) -> None:
        """Pick the search form and translate it into the UI's filter schema."""
        search_form = self._pick_search_form(forms)
        if not search_form:
            self.stdout.write(self.style.WARNING(
                '  no obvious search form — filter_schema.json not written'
            ))
            return

        fields = []
        for f in search_form['fields']:
            if f['type'] in BORING_TYPES or (f['name'] or '') in BORING_FIELDS:
                continue
            if not f['name']:
                continue
            entry = {
                'name': f['name'],
                'label': f['label'] or f['name'],
                'type': 'select' if f['tag'] == 'select' else (f['type'] or 'text'),
            }
            if f['options']:
                entry['choices'] = [
                    o for o in f['options'] if o['value'] not in ('', 'All')
                ]
            fields.append(entry)

        self._write(out_dir / 'filter_schema.json', {
            'source_url': search_form.get('action'),
            'method': search_form.get('method'),
            'form_id': search_form.get('id'),
            'fields': fields,
        })
        self.stdout.write(
            f'  filter_schema.json ({len(fields)} filter field(s)) '
            f'[method={search_form.get("method")}]'
        )

    def _pick_search_form(self, forms: list[dict]) -> dict | None:
        """Prefer a Drupal Views exposed form; else the one with most filters."""
        def usable(form):
            return [
                f for f in form['fields']
                if f['type'] not in BORING_TYPES and (f['name'] or '') not in BORING_FIELDS
            ]

        exposed = [
            f for f in forms
            if 'views-exposed-form' in (f.get('classes') or '')
            or 'views-exposed-form' in (f.get('id') or '')
        ]
        pool = exposed or forms
        pool = [f for f in pool if usable(f)]
        return max(pool, key=lambda f: len(usable(f)), default=None)

    def _dump_repeats(self, page, out_dir) -> None:
        repeats = page.evaluate(FIND_REPEATS_JS)
        self._write(out_dir / 'repeats.json', {'candidates': repeats})
        self.stdout.write(f'  repeats.json ({len(repeats)} repeated structure(s))')
        for r in repeats[:3]:
            self.stdout.write(f'      {r["count"]:>4} x  {r["selector"][:100]}')

    def _capture_action(self, page, requests: list, out_dir) -> None:
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            '  ACTION CAPTURE\n'
            '  In the open browser, click the contact/invite control for\n'
            '  exactly ONE candidate and complete it. This sends a real\n'
            '  message to a real person, so pick deliberately.\n'
        ))
        marker = len(requests)
        input('  Press Enter here when finished (or immediately to skip)... ')

        captured = requests[marker:]
        writes = [r for r in captured if r['method'] not in ('GET', 'HEAD')]
        self._write(out_dir / 'action_requests.json', {
            'all': captured, 'writes': writes,
        })
        self.stdout.write(
            f'  action_requests.json ({len(writes)} write request(s) '
            f'of {len(captured)} captured)'
        )

    def _write(self, path, data) -> None:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
