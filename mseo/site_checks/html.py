"""A small stdlib HTML reader for the audit: title, metas, H1s, canonical, images, links, JSON-LD and scripts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class Page:
    title: str | None = None
    metas: dict[str, str] = field(default_factory=dict)  # lower-cased name -> content (first wins)
    h1s: list[str] = field(default_factory=list)
    canonical: str | None = None
    images: list[dict] = field(default_factory=list)  # {"src", "alt"} (alt None when the attribute is absent)
    links: list[str] = field(default_factory=list)  # href values of <a>, in order
    json_ld: list[str] = field(default_factory=list)  # raw script bodies
    inline_scripts: int = 0
    external_scripts: int = 0
    robots_meta: str | None = None

    def schema_types(self) -> tuple[set[str], list[str]]:
        """(every @type found in the JSON-LD blocks, including @graph members; parse errors)."""
        types: set[str] = set()
        errors: list[str] = []
        for i, raw in enumerate(self.json_ld, 1):
            try:
                data = json.loads(raw)
            except ValueError as exc:
                errors.append(f"JSON-LD block {i} does not parse ({exc})")
                continue
            for node in _nodes(data):
                t = node.get("@type")
                for name in (t if isinstance(t, list) else [t]):
                    if isinstance(name, str):
                        types.add(name)
        return types, errors


def _nodes(data):
    if isinstance(data, list):
        for item in data:
            yield from _nodes(item)
    elif isinstance(data, dict):
        yield data
        if isinstance(data.get("@graph"), list):
            yield from _nodes(data["@graph"])


class _Reader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.page = Page()
        self._in: str | None = None  # "title", "h1", "ld", "script"
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        if tag == "title" and self.page.title is None:
            self._in, self._buf = "title", []
        elif tag == "h1":
            self._in, self._buf = "h1", []
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name and name not in self.page.metas:
                self.page.metas[name] = a.get("content", "")
            if name == "robots":
                self.page.robots_meta = a.get("content", "")
        elif tag == "link" and "canonical" in (a.get("rel") or "").lower().split() and self.page.canonical is None:
            self.page.canonical = a.get("href")
        elif tag == "img":
            self.page.images.append({"src": a.get("src", ""), "alt": a["alt"] if "alt" in a else None})
        elif tag == "a" and a.get("href"):
            self.page.links.append(a["href"])
        elif tag == "script":
            if (a.get("type") or "").lower().strip() == "application/ld+json":
                self._in, self._buf = "ld", []
            elif a.get("src"):
                self.page.external_scripts += 1
            else:
                self.page.inline_scripts += 1
                self._in = "script"

    def handle_endtag(self, tag):
        if self._in == "title" and tag == "title":
            self.page.title = "".join(self._buf).strip()
        elif self._in == "h1" and tag == "h1":
            self.page.h1s.append(" ".join("".join(self._buf).split()))
        elif self._in == "ld" and tag == "script":
            self.page.json_ld.append("".join(self._buf))
        elif self._in == "script" and tag == "script":
            pass
        else:
            return
        self._in = None

    def handle_data(self, data):
        if self._in in ("title", "h1", "ld"):
            self._buf.append(data)


def read(body: str) -> Page:
    reader = _Reader()
    reader.feed(body)
    reader.close()
    return reader.page
