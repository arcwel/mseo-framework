"""Loaded only by the isolation suite (its folder is put on PYTHONPATH for the case's subprocess).

1. Installs an audit hook that logs every path opened or listed and every subprocess argv, one JSON line each,
   to the file named by MSEO_AUDIT_LOG.
2. Swaps mseo.secrets.Keychain's methods for a JSON-file keychain at MSEO_FAKE_KEYCHAIN, so a case never touches
   the real Keychain. This file is test code: the framework itself has no switch that turns a fake on.
"""

import json
import os
import sys

_LOG = os.environ.get("MSEO_AUDIT_LOG")
_FD = os.open(_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600) if _LOG else None
_EVENTS = {"open", "os.listdir", "os.scandir", "subprocess.Popen", "os.chdir", "os.rename", "os.remove", "os.mkdir",
           "shutil.copyfile", "shutil.rmtree", "os.replace"}


def _hook(event, args):
    if _FD is None or event not in _EVENTS:
        return
    try:
        if event == "subprocess.Popen":
            rec = {"event": event, "executable": str(args[0]), "argv": [str(a) for a in (args[1] or [])], "cwd": os.getcwd()}
        else:
            first = args[0] if args else None
            if isinstance(first, int):
                return  # an fd, already logged when opened
            rec = {"event": event, "path": os.fsdecode(first) if first is not None else ".", "cwd": os.getcwd()}
            if event in ("os.rename", "os.replace", "shutil.copyfile") and len(args) > 1:
                rec["dest"] = os.fsdecode(args[1])
        os.write(_FD, (json.dumps(rec) + "\n").encode())
    except Exception:  # noqa: BLE001 - logging must never change the command's behavior
        pass


sys.addaudithook(_hook)

_KEYCHAIN = os.environ.get("MSEO_FAKE_KEYCHAIN")
if _KEYCHAIN:
    import mseo.secrets as _secrets

    def _load():
        try:
            with open(_KEYCHAIN, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save(items):
        with open(_KEYCHAIN, "w", encoding="utf-8") as fh:
            json.dump(items, fh)

    def _key(service, account):
        return f"{service}|{account}"

    def exists(self, service, account):
        return _key(service, account) in _load()

    def get(self, service, account):
        items = _load()
        if _key(service, account) not in items:
            raise _secrets.MissingCredential(f"no Keychain item {service} (account {account})")
        return items[_key(service, account)]

    def set_(self, service, account, payload, replace=False):
        items = _load()
        if _key(service, account) in items and not replace:
            raise _secrets.SecretExists(f"Keychain item {service} exists; pass --replace to overwrite it")
        items[_key(service, account)] = payload
        _save(items)

    _secrets.Keychain.exists, _secrets.Keychain.get, _secrets.Keychain.set = exists, get, set_

# 3. No network in an isolation case: WordPress and link checks answer from memory. Also test code only.
if os.environ.get("MSEO_FAKE_NET"):
    import mseo.cms.wordpress as _wp
    import mseo.content.check as _check
    import mseo.content.links as _links
    import mseo.publish as _publish

    def _ok(url, timeout=20):
        return {"url": url, "status": 200, "ok": True}

    _posted = {}

    def _request(site, user, pw, method, path, payload=None, query=None):
        if path == "wp/v2/users/me":
            return 200, {"id": 1, "name": user, "roles": ["editor"], "capabilities": {}}
        if method == "POST":
            _posted.update(payload, id=42, link=f"{site}/{payload.get('slug', 'x')}/")
            return 201, dict(_posted)
        if path.startswith("wp/v2/posts/"):
            return 200, {**_posted, "content": {"raw": _posted.get("content", "")}}
        return 200, []

    _wp.request = _request
    _wp.public_status = lambda url: 200
    _wp.fetch_published = lambda site: [{"id": 1, "slug": "a", "date": "2026-11-01T09:00:00", "status": "publish",
                                         "title": {"rendered": "A"}, "content": {"rendered": "B"}, "link": site + "/a/"}]
    _check.check_url = _links.check_url = _publish.check_url = _ok
