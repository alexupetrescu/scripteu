"""Everything that knows what the ESC portal looks like lives in this package.

``auth``    — EU Login session capture and reuse (portal-agnostic).
``adapter`` — the only module holding selectors, field names and page flow.

If the portal is redesigned, ``adapter`` is the file to fix.
"""
