# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bing-Web engine variant that sends ONLY the ``q`` parameter.

The stock ``bing`` engine adds ``adlt`` / ``setlang`` / ``cc`` / ``mkt``
parameters.  As of 2026-10-11 Bing answers requests carrying any of those
*known* parameters with a JavaScript-only shell that contains no
server-rendered results (``b_algo`` count = 0).  A bare ``?q=...`` request
still returns the classic server-rendered HTML (80-96 ``b_algo`` blocks),
so this variant strips every extra parameter.

Unknown parameters (e.g. ``foo=bar``) are ignored by Bing and do not break
the response -- only the recognised ones do.

Response parsing is identical to the stock engine.
"""

import base64
import typing as t
from urllib.parse import parse_qs, quote, urlparse

from searx.utils import eval_xpath, eval_xpath_getindex, eval_xpath_list, extract_text

if t.TYPE_CHECKING:
    from searx.extended_types import SXNG_Response
    from searx.search.processors import OnlineParams

about: dict[str, t.Any] = {
    "website": "https://www.bing.com",
    "wikidata_id": "Q182496",
    "official_api_documentation": "https://github.com/MicrosoftDocs/bing-docs",
    "use_official_api": False,
    "require_api_key": False,
    "results": "HTML",
}

categories = ["general", "web"]
paging = False
safesearch = False
enable_http3 = False

base_url = "https://www.bing.com"
"""Bing-Web search URL"""


def request(query: str, params: "OnlineParams"):
    """Assemble a minimal Bing-Web request: only ``q`` is sent.

    Spaces are encoded as ``%20`` (not ``+``): Bing answers ``+``-encoded
    queries with the JavaScript-only shell (0 ``b_algo``) but returns the
    classic server-rendered HTML for ``%20`` (verified 2026-10-11).
    """
    params["url"] = f"{base_url}/search?q={quote(query, safe='')}"


def response(resp: "SXNG_Response") -> list[dict[str, t.Any]]:
    """Get response from Bing-Web"""
    results: list[dict[str, t.Any]] = []

    dom = resp.html()

    for item in eval_xpath_list(dom, '//ol[@id="b_results"]/li[contains(@class, "b_algo")]'):
        link = eval_xpath_getindex(item, ".//h2/a", 0, None)
        if link is None:
            continue

        href = link.attrib.get("href", "")
        title = extract_text(link)

        if not href or not title:
            continue

        if href.startswith("https://www.bing.com/ck/a?"):
            qs = parse_qs(urlparse(href).query)
            u_values = qs.get("u")
            if u_values:
                u_val = u_values[0]
                if u_val.startswith("a1"):
                    encoded = u_val[2:]
                    # base64url without padding
                    encoded += "=" * (-len(encoded) % 4)
                    href = base64.urlsafe_b64decode(encoded).decode("utf-8", errors="replace")

        # remove decorative icons that Bing injects into <p> elements
        content_els = eval_xpath(item, ".//p")
        for p in content_els:
            for icon in p.xpath('.//span[@class="algoSlug_icon"]'):
                icon.getparent().remove(icon)
        content = extract_text(content_els)

        results.append({"url": href, "title": title, "content": content})

    return results
