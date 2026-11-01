"""Authored parser examples; none of this HTML contains official game rules."""

from holdet_lib.rule_pages import fetch_rule_page
from tests.test_decision_analysis import GAME, NOW


DOCUMENT = "https://cdn.holdet.dk/nexus/fantasy-app-rule-pages/synthetic-test-rules.html"
HTML = """<!doctype html>
<html lang="en"><head>
<style>.ignored-style-marker { color: purple; }</style>
</head><body>
<h1>Synthetic parser examples</h1>
<p>This document is written solely for automated tests. Its invented events,
scores, and conditions do not describe any real competition. Tests use these
examples to check extraction, provenance, and changes to saved evidence without
redistributing a third-party page or fetching anything over the network.</p>
<table><tr><th>Invented event</th><th>Example score</th></tr>
<tr><td>Blue token</td><td>11</td></tr></table>
<table><tr><td>Orange token</td><td>-3</td></tr></table>
<section class="collapsible-panel">
  <button class="collapsible">Participation example</button>
  <div class="collapsible-content" hidden>
    Synthetic players receive an invented award after <strong>17 example minutes</strong>.
    This deliberately hidden condition exercises nested markup and Unicode æøå.
  </div>
</section>
<section class="collapsible-panel">
  <button class="collapsible">Transfer example</button>
  <div class="collapsible-content" style="display:none">
    An imaginary exchange costs two blue tokens. These authored words test
    collapsed text extraction and do not establish a real transfer allowance.
  </div>
</section>
<details><summary>Native example</summary>
  <p>A closed native section still contains evidence. Its <em>nested content</em>
  must be extracted without needing browser interaction.</p>
</details>
<script>const ignoredScriptMarker = "not rule evidence";</script>
</body></html>"""


def capture(html=HTML, *, now=NOW):
    return fetch_rule_page(GAME, fetch_text=lambda url: (
        f'<iframe src="{DOCUMENT}"></iframe>' if url.endswith("/rules") else html
    ), now=now)
