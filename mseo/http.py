"""HTTP transport shared by every client that sends credentials (WordPress now, Google in M2a)."""

from __future__ import annotations

import ssl
import urllib.request


class RedirectRefused(Exception):
    """A server answered with a redirect on a call that carries credentials."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Authenticated calls never follow a redirect: urllib would resend the Authorization header to the new host,
    even over plain http. Subclasses set error_type and hint for their own error and advice."""

    error_type: type[Exception] = RedirectRefused
    hint: str = ""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        text = f"{req.full_url} answered {code} redirect to {newurl}; credentials are never sent on a redirect."
        raise self.error_type(f"{text} {self.hint}".strip())


def opener(handler: type[NoRedirect] = NoRedirect) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(handler, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
