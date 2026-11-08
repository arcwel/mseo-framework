"""content/places: the city, region and postal code of a location, read from customer.json or facts.json.

One rule for every module that needs the city (variation, claims, new, the WordPress business schema): an explicit
`city` on the location wins; otherwise it is the token before ", ST zip" in the address, so "<number> <street>, Suite 4,
Townsville, ST 00000" gives "Townsville". An address the regex does not read falls back to its second comma part.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

ADDRESS_RE = re.compile(r"^(?P<street>.+?),\s*(?P<city>[^,]+?),\s*(?P<region>[A-Z]{2})\s+(?P<postal>\d{5}(?:-\d{4})?)$")


def address_parts(text: str) -> dict | None:
    """{street, city, region, postal} for '<street>, <city>, <ST> <zip>', else None."""
    m = ADDRESS_RE.match((text or "").strip())
    return m.groupdict() if m else None


def city_of_address(text: str) -> str:
    parts = address_parts(text)
    if parts:
        return parts["city"].strip()
    pieces = [p.strip() for p in (text or "").split(",")]
    return pieces[1] if len(pieces) >= 2 and pieces[1] else ""


def location_city(location: dict | None) -> str:
    """The location's `city` when set, else the city derived from its address."""
    if not isinstance(location, Mapping):
        return ""
    explicit = str(location.get("city") or "").strip()
    return explicit or city_of_address(str(location.get("address") or ""))


def place_words(location: dict) -> set[str]:
    """The words that mark a street as an address of this location: city, region and postal code (and `city`)."""
    parts = address_parts(str(location.get("address") or ""))
    words = set()
    if parts:
        words |= {parts["city"].strip(), parts["region"], parts["postal"]}
    else:
        for p in str(location.get("address") or "").split(",")[1:]:
            p = p.strip()
            words.update([p] if not re.search(r"\d", p) else p.split())
    if location.get("city"):
        words.add(str(location["city"]).strip())
    return {w for w in words if w}
