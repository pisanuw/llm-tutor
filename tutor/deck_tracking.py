"""The deck tracking tag, added to every Reveal deck the app serves
(step 1.6).

A rendered deck names only the vendored Reveal files (it opens from a file
too, and tests/test_no_third_party.py holds it to that), so the tag is not
part of the render: the ``/slides`` mount (tutor/main.py) adds it when it
serves a deck's ``index.html``, which also instruments the 40 decks in the
current slide seed without a re-render or a new release asset. A deck that
already carries a tag (a hand-edited one) gets it replaced by the versioned
one. Nothing is added while ``ACTIVITY_EVENTS`` is off.
"""

from __future__ import annotations

import re

from tutor.templating import asset_url

_EXISTING_TAG = re.compile(r'<script[^>]*src="[^"]*deck-track\.js[^"]*"[^>]*>\s*</script>\s*', re.I)
_BODY_END = re.compile(r"</body\s*>", re.I)


def tracking_tag() -> str:
    """The versioned script tag with the heartbeat interval."""
    from tutor.config import get_config

    cfg = get_config()
    return (f'<script src="{asset_url("deck-track.js")}" '
            f'data-heartbeat="{cfg["activity_heartbeat_s"]}"></script>')


def inject(html: str) -> str:
    """``html`` with exactly one current tracking tag before ``</body>``, or
    unchanged (an existing tag removed) while tracking is off."""
    from tutor.config import get_config

    html = _EXISTING_TAG.sub("", html)
    if not get_config()["activity_events"]:
        return html
    tag = tracking_tag() + "\n"
    match = None
    for match in _BODY_END.finditer(html):
        pass   # the last </body>
    if match is None:
        return html + "\n" + tag
    return html[: match.start()] + tag + html[match.start():]
