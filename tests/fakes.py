"""Test doubles. A fake keychain is passed in as a constructor argument; no environment variable switches it on."""

from __future__ import annotations

import json

from mseo.secrets import MissingCredential, SecretExists


class FakeKeychain:
    """Same interface as mseo.secrets.Keychain, held in memory. `calls` records every (op, service, account)."""

    def __init__(self, items: dict | None = None):
        self.items: dict[tuple[str, str], str] = {k: json.dumps(v) for k, v in (items or {}).items()}
        self.calls: list[tuple[str, str, str]] = []

    def exists(self, service: str, account: str) -> bool:
        self.calls.append(("exists", service, account))
        return (service, account) in self.items

    def get(self, service: str, account: str) -> dict:
        self.calls.append(("get", service, account))
        if (service, account) not in self.items:
            raise MissingCredential(f"no Keychain item {service} (account {account})")
        return json.loads(self.items[(service, account)])

    def set(self, service: str, account: str, payload: dict, replace: bool = False) -> None:
        self.calls.append(("set", service, account))
        if (service, account) in self.items and not replace:
            raise SecretExists(f"Keychain item {service} exists")
        self.items[(service, account)] = json.dumps(payload)


class FakeWordPress:
    """Stands in for mseo.cms.wordpress.request / public_status / fetch_published. Records every call's site, so a
    test can prove which site a command talked to. Posts live in memory, keyed by slug."""

    def __init__(self, link_base: str):
        self.link_base = link_base.rstrip("/")
        self.sites: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.posts: dict[str, dict] = {}
        self.pages: dict[str, dict] = {}
        self.snippets: dict[int, dict] = {}
        self.snippet_mangle = False  # set to make the site store different code than it was sent
        self.next_id = 42
        self.fail_with = None  # set to a WPError to make every request raise it

    def install(self, monkeypatch, wordpress_module) -> "FakeWordPress":
        monkeypatch.setattr(wordpress_module, "request", self.request)
        monkeypatch.setattr(wordpress_module, "public_status", lambda url: 200)
        monkeypatch.setattr(wordpress_module, "fetch_published", self.fetch_published)
        return self

    def request(self, site, user, pw, method, path, payload=None, query=None):
        self.sites.append(site)
        self.calls.append((method, path))
        if self.fail_with:
            raise self.fail_with
        if path == "wp/v2/users/me":
            return 200, {"id": 1, "name": user, "roles": ["editor"], "capabilities": {"edit_posts": True}}
        if method == "GET" and path in ("wp/v2/posts", "wp/v2/pages", "wp/v2/elementor_snippet"):
            slug = (query or {}).get("slug")
            return 200, [p for p in self.posts.values() if p["slug"] == slug] if slug else []
        if method == "POST" and path.startswith("wp/v2/posts"):
            post_id = int(path.rsplit("/", 1)[1]) if path.count("/") == 3 else self.next_id
            self.next_id += post_id == self.next_id
            self.posts[payload["slug"]] = {"id": post_id, "slug": payload["slug"], "status": payload["status"],
                                           "content": {"raw": payload["content"]},
                                           "link": f"{self.link_base}/{payload['slug']}/"}
            return 201, self.posts[payload["slug"]]
        if method == "GET" and path.startswith("wp/v2/posts/"):
            post_id = int(path.rsplit("/", 1)[1])
            return 200, next(p for p in self.posts.values() if p["id"] == post_id)
        if method == "POST" and path.startswith("wp/v2/pages"):
            page_id = int(path.rsplit("/", 1)[1]) if path.count("/") == 3 else 7
            self.pages[payload["slug"]] = {"id": page_id, "slug": payload["slug"], "status": payload["status"],
                                           "content": {"raw": payload["content"]}, "link": f"{self.link_base}/{payload['slug']}/"}
            return 201, self.pages[payload["slug"]]
        if method == "GET" and path.startswith("wp/v2/pages/"):
            page_id = int(path.rsplit("/", 1)[1])
            return 200, next(p for p in self.pages.values() if p["id"] == page_id)
        if method == "POST" and path.startswith("wp/v2/elementor_snippet"):
            sid = int(path.rsplit("/", 1)[1]) if path.count("/") == 3 else 9
            self.snippets[sid] = {"id": sid, "status": payload["status"], "title": {"raw": payload["title"]},
                                  "meta": dict(payload["meta"]), "link": f"{self.link_base}/?p={sid}"}
            if self.snippet_mangle:
                self.snippets[sid]["meta"]["_elementor_code"] = "/* stripped by a filter */"
            return 201, self.snippets[sid]
        if method == "GET" and path.startswith("wp/v2/elementor_snippet/"):
            return 200, self.snippets[int(path.rsplit("/", 1)[1])]
        return 201, {"id": 7, "link": f"{self.link_base}/x/"}

    def fetch_published(self, site):
        self.sites.append(site)
        return [{"id": 1, "slug": "a-post", "date": "2026-11-01T09:00:00", "status": "publish",
                 "title": {"rendered": "A &amp; post"}, "content": {"rendered": "<p>Body&nbsp;text</p>"},
                 "link": f"{site}/a-post/"}]
