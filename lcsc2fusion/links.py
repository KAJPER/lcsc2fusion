"""Links to the source of a part. One place, used by the add-in window and the MCP server."""

from __future__ import annotations

import re

LCSC_PRODUCT_URL_PREFIX = "https://www.lcsc.com/product-detail/"
LCSC_PRODUCT_URL_SUFFIX = ".html"
_LCSC_ID_RE = re.compile(r"C[0-9]{1,12}")


def lcsc_product_url(lcsc_id: str) -> str:
    """Product page of an LCSC part, e.g. https://www.lcsc.com/product-detail/C2040.html.

    LCSC redirects this short form to the full page with the part name in the address.
    Raises ValueError for anything that is not a canonical id (letter C and digits), so a
    caller can never build a link from free text.
    """
    if _LCSC_ID_RE.fullmatch(lcsc_id) is None:
        raise ValueError(f"not a canonical LCSC id: {lcsc_id!r}")
    return f"{LCSC_PRODUCT_URL_PREFIX}{lcsc_id}{LCSC_PRODUCT_URL_SUFFIX}"
