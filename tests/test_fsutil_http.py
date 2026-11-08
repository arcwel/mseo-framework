"""M2 step 1: one atomic JSON writer, one file hash, one redirect refusal, shared by every module."""

import hashlib
import json
import urllib.request

import pytest

from mseo import fsutil, http
from mseo.cms import wordpress


def test_write_json_atomic_creates_parents_and_leaves_no_temp(tmp_path):
    target = tmp_path / "a" / "b" / "record.json"
    fsutil.write_json_atomic(target, {"x": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"x": 1}
    assert target.read_text(encoding="utf-8").endswith("\n")
    assert [p.name for p in target.parent.iterdir()] == ["record.json"]


def test_write_json_atomic_keeps_the_old_file_when_serialising_fails(tmp_path):
    target = tmp_path / "record.json"
    fsutil.write_json_atomic(target, {"old": True})
    with pytest.raises(TypeError):
        fsutil.write_json_atomic(target, {"bad": object()})
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert [p.name for p in tmp_path.iterdir()] == ["record.json"]


def test_sha256_file_hashes_bytes_and_returns_none_when_missing(tmp_path):
    f = tmp_path / "data.csv"
    f.write_bytes(b"a,b\n1,2\n")
    assert fsutil.sha256_file(f) == hashlib.sha256(b"a,b\n1,2\n").hexdigest()
    assert fsutil.sha256_file(tmp_path / "missing") is None


def test_no_redirect_refuses_with_a_neutral_error():
    req = urllib.request.Request("https://api.example.test/v1/thing")
    with pytest.raises(http.RedirectRefused, match="redirect"):
        http.NoRedirect().redirect_request(req, None, 302, "Found", {}, "http://other.test/steal")


def test_opener_uses_no_redirect():
    assert any(isinstance(h, http.NoRedirect) for h in http.opener().handlers)


def test_wordpress_reuses_the_shared_handler():
    assert issubclass(wordpress.NoRedirect, http.NoRedirect)
    assert wordpress.NoRedirect.error_type is wordpress.WPError


def test_old_helpers_are_gone():
    """The copies M1 grew are replaced by fsutil, so there is one writer and one hash to fix."""
    from mseo import doctor, seal
    from mseo.capture import account
    from mseo.content import check
    assert seal.sha256_file is fsutil.sha256_file
    assert check.sha256_file is fsutil.sha256_file
    assert not hasattr(seal, "_write_json")
    assert account.write_record.__module__ == "mseo.capture.account"  # thin wrapper kept for its callers
    assert doctor.write_record.__module__ == "mseo.doctor"
