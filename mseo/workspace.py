"""The workspace object: the only way a command gets customer data.

`open(slug)` resolves the folder, runs the cwd guard, validates customer.json against its versioned schema, fills
defaults and returns a frozen Workspace whose `path()` refuses to leave the workspace.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator, FormatChecker

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
SCHEMA_VERSION_RE = re.compile(r"^customer/(\d+)$")
RETIRED = {1: "customer/1 is no longer read: move to customer/2, which adds identity (email, kind, chrome_profile), "
              "a top-level timezone (moved from posting), schedule.weekly and per-source adapter and check; "
              "account-level chrome_profile moves into identity"}
SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
DEFAULT_ROOT = "~/Projects/mseo-workspaces"
CONFIG_FILE = "customer.json"


class WorkspaceError(Exception):
    """Base class: every subclass maps to exit 3 with a one-line reason."""


class ConfigInvalid(WorkspaceError):
    """customer.json is missing, unreadable or fails the schema; or the slug/folder is not acceptable."""


class WorkspaceMismatch(WorkspaceError):
    """The current directory is inside a different workspace than the one named."""


class OutsideWorkspace(WorkspaceError):
    """A path would resolve outside the workspace root."""


def workspaces_root() -> Path:
    return Path(os.environ.get("MSEO_WORKSPACES") or DEFAULT_ROOT).expanduser().resolve()


def _check_slug(slug: str) -> None:
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ConfigInvalid(f"bad workspace slug {slug!r}: must match {SLUG_RE.pattern}")


def workspace_dir(slug: str, root: Path | None = None) -> Path:
    """The real folder of `slug`, which must sit directly under the workspaces root (no symlinks out or across)."""
    _check_slug(slug)
    root = (root or workspaces_root()).resolve()
    folder = root / slug
    if not folder.exists() and not folder.is_symlink():
        raise ConfigInvalid(f"no workspace {slug!r} under {root}")
    real = folder.resolve()
    if real.parent != root or real.name != slug:
        raise ConfigInvalid(f"workspace {slug!r} must be a real folder directly under {root}, not a link to {real}")
    if not real.is_dir():
        raise ConfigInvalid(f"workspace {slug!r} is not a folder")
    return real


def workspace_containing(path: Path, root: Path | None = None) -> str | None:
    """The slug of the workspace `path` sits in, or None when it is outside every workspace."""
    root = (root or workspaces_root()).resolve()
    try:
        rel = Path(path).resolve().relative_to(root)
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


def cwd_guard(slug: str, cwd: Path | None = None, root: Path | None = None) -> None:
    here = Path(cwd) if cwd is not None else Path.cwd()
    inside = workspace_containing(here, root)
    if inside is not None and inside != slug:
        raise WorkspaceMismatch(f"workspace_mismatch: cwd is inside {inside}, --workspace is {slug}")


# ---------------------------------------------------------------- schema

@lru_cache(maxsize=None)
def load_schema(version: int) -> dict:
    path = SCHEMA_DIR / f"customer.v{version}.json"
    if not path.exists():
        raise ConfigInvalid(f"schema_version customer/{version} is not known to this framework (no {path.name})")
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _validator(version: int) -> Draft202012Validator:
    return Draft202012Validator(load_schema(version), format_checker=FormatChecker())


def _resolve(schema: dict, root: dict) -> dict:
    ref = schema.get("$ref")
    if ref and ref.startswith("#/$defs/"):
        return {**root["$defs"][ref.split("/")[-1]], **{k: v for k, v in schema.items() if k != "$ref"}}
    return schema


def _fill_defaults(value: Any, schema: dict, root: dict) -> Any:
    schema = _resolve(schema, root)
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name, sub in props.items():
            sub = _resolve(sub, root)
            if name not in value and "default" in sub:
                value[name] = copy.deepcopy(sub["default"])
            if name in value:
                value[name] = _fill_defaults(value[name], sub, root)
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            for name in list(value):
                if name not in props and not name.startswith("_"):
                    value[name] = _fill_defaults(value[name], extra, root)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        value = [_fill_defaults(v, schema["items"], root) for v in value]
    return value


def identifying_paths(schema: dict) -> list[str]:
    """Dotted paths of every field marked x-identifying, e.g. 'locations[].address', 'accounts.*.id'."""
    out: list[str] = []

    def walk(node: dict, path: str) -> None:
        node = _resolve(node, schema)
        if node.get("x-identifying"):
            out.append(path + ("[]" if node.get("type") == "array" else ""))
        for name, sub in node.get("properties", {}).items():
            walk(sub, f"{path}.{name}" if path else name)
        if isinstance(node.get("additionalProperties"), dict):
            walk(node["additionalProperties"], f"{path}.*")
        if isinstance(node.get("items"), dict) and not node.get("x-identifying"):
            walk(node["items"], f"{path}[]")

    walk(schema, "")
    return out


def identifying_values(config: dict, schema: dict) -> list[tuple[str, str]]:
    """[(path, value)] for every x-identifying string in `config`."""
    out: list[tuple[str, str]] = []

    def take(value: Any, parts: list[str], where: str) -> None:
        if not parts:
            for v in value if isinstance(value, list) else [value]:
                if isinstance(v, str):
                    out.append((where, v))
            return
        head, rest = parts[0], parts[1:]
        if head.endswith("[]"):
            head = head[:-2]
            items = value.get(head, []) if isinstance(value, dict) else []
            for i, item in enumerate(items if isinstance(items, list) else []):
                take(item, rest, f"{where}.{head}[{i}]".lstrip("."))
        elif head == "*":
            for k, v in (value.items() if isinstance(value, dict) else []):
                if not k.startswith("_"):
                    take(v, rest, f"{where}.{k}".lstrip("."))
        elif isinstance(value, dict) and head in value:
            take(value[head], rest, f"{where}.{head}".lstrip("."))

    for path in identifying_paths(schema):
        take(config, path.split("."), "")
    return out


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _relpath_ok(rel: str) -> bool:
    p = Path(rel)
    return not p.is_absolute() and ".." not in p.parts


def _is_placeholder(err) -> bool:
    """A `<...>` value from the template, whichever rule it tripped (the `not` pattern, a format, an enum)."""
    inst = err.instance
    if isinstance(inst, str):
        return inst.startswith("<") and inst.endswith(">")
    if isinstance(inst, dict) and err.validator in ("additionalProperties", "propertyNames"):
        return any(k.startswith("<") and k.endswith(">") for k in inst)
    return False


def _file_order(raw: dict) -> dict[str, int]:
    """Position of every JSON path in the document, so messages list fields in the order the operator reads them."""
    order: dict[str, int] = {}

    def walk(node, path):
        order[path] = len(order)
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(raw, "$")
    return order


def _describe(raw: dict, errors: list) -> str:
    """One line per problem, in file order. Placeholders (`<...>` values) are the common case for a new workspace, so
    they get a plain instruction instead of the schema's `not` wording."""
    order = _file_order(raw)
    errors = sorted(errors, key=lambda e: order.get(e.json_path, len(order)))
    placeholders = [e for e in errors if _is_placeholder(e)]
    others = [e for e in errors if not _is_placeholder(e)]
    lines = []
    if placeholders:
        lines.append(f"{CONFIG_FILE}: {len(placeholders)} value(s) still need filling in")
        lines += [f"  {e.json_path}: replace the <...> placeholder" for e in placeholders]
        lines.append(f"run: mseo workspace init {raw.get('slug', '<slug>')}")
    if others and placeholders:
        lines.append(f"{CONFIG_FILE}: {len(others)} value(s) do not match the schema")
        lines += [f"  {e.json_path}: {e.message}" for e in others]
    elif others:
        more = f" (and {len(others) - 1} more)" if len(others) > 1 else ""
        lines.append(f"{CONFIG_FILE} {others[0].json_path}: {others[0].message}{more}")
    return "\n".join(lines)


def validate(raw: Any, slug: str) -> dict:
    """The validated config with defaults filled, or ConfigInvalid naming the JSON path."""
    if not isinstance(raw, dict):
        raise ConfigInvalid(f"{CONFIG_FILE} must be a JSON object")
    m = SCHEMA_VERSION_RE.match(str(raw.get("schema_version", "")))
    if not m:
        raise ConfigInvalid(f"$.schema_version must look like customer/N, got {raw.get('schema_version')!r}")
    version = int(m.group(1))
    if version in RETIRED:
        raise ConfigInvalid(RETIRED[version])
    schema = load_schema(version)
    errors = list(_validator(version).iter_errors(raw))
    if errors:
        raise ConfigInvalid(_describe(raw, errors))
    config = _fill_defaults(copy.deepcopy(raw), schema, schema)
    if config["slug"] != slug:
        raise ConfigInvalid(f"$.slug is {config['slug']!r} but the folder is {slug!r}")
    cms, business = config["cms"], config["business"]
    if cms["site"].rstrip("/") != business["site"].rstrip("/") and not cms["allow_other_site"]:
        raise ConfigInvalid("$.cms.site must equal $.business.site (set cms.allow_other_site to allow another site)")
    try:
        ZoneInfo(config["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigInvalid(f"$.timezone {config['timezone']!r} is not an IANA zone") from exc
    for key, rel in config["files"].items():
        if not key.startswith("_") and not _relpath_ok(rel):
            raise ConfigInvalid(f"$.files.{key} must be workspace-relative, got {rel!r}")
    return config


# ---------------------------------------------------------------- the object

@dataclass(frozen=True)
class Workspace:
    slug: str
    root: Path
    config: MappingProxyType
    keychain: Any = field(default=None, repr=False, compare=False)

    def path(self, *parts: str | os.PathLike) -> Path:
        """Join under the workspace root and resolve; raise OutsideWorkspace if the real path escapes it."""
        target = self.root.joinpath(*parts).resolve()
        if target != self.root and self.root not in target.parents:
            raise OutsideWorkspace(f"path {Path(*parts)} resolves to {target}, outside workspace {self.slug}")
        return target

    def file(self, key: str) -> Path:
        """A configured workspace file (files.<key>) as a checked path."""
        return self.path(self.config["files"][key])

    def contains(self, path: str | os.PathLike) -> Path:
        """`path` (absolute, or relative to cwd) as a real path inside this workspace, or OutsideWorkspace."""
        real = Path(path).resolve()
        if real != self.root and self.root not in real.parents:
            raise OutsideWorkspace(f"{path} is outside workspace {self.slug}")
        return real

    def account(self, source: str) -> str:
        acct = self.config["accounts"].get(source)
        if not acct:
            raise ConfigInvalid(f"workspace {self.slug} has no account for {source!r}")
        return acct["id"]

    def secret(self, source: str) -> dict:
        """The parsed JSON payload of Keychain item mseo.<slug>.<source>."""
        from mseo import secrets

        return secrets.workspace_secret(self, source)

    def identity_for(self, source: str) -> dict:
        """The identity a pull of `source` runs as: the account's temporary override, else the workspace identity."""
        acct = self.config["accounts"].get(source)
        if not acct:
            raise ConfigInvalid(f"workspace {self.slug} has no account for {source!r}")
        over = acct.get("identity_override")
        if over:
            return {"email": over["email"], "chrome_profile": over["chrome_profile"], "allow_shared": True,
                    "chrome_device": over.get("chrome_device"), "override": True, "until": over["until"]}
        ident = self.config["identity"]
        return {"email": ident["email"], "chrome_profile": ident["chrome_profile"],
                "allow_shared": ident["allow_shared"], "chrome_device": ident.get("chrome_device"), "override": False,
                "until": None}

    def enabled_sources(self) -> list[str]:
        return sorted(k for k, v in self.config["accounts"].items() if not k.startswith("_") and v.get("enabled", True))



def open(slug: str, *, cwd: Path | None = None, keychain=None) -> Workspace:  # noqa: A001 - the design's name
    root = workspaces_root()
    _check_slug(slug)
    cwd_guard(slug, cwd, root)
    folder = workspace_dir(slug, root)
    try:
        raw = json.loads((folder / CONFIG_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigInvalid(f"{folder / CONFIG_FILE} could not be read ({exc})") from exc
    config = validate(raw, slug)
    if keychain is None:
        from mseo.secrets import Keychain

        keychain = Keychain()
    return Workspace(slug, folder, _freeze(config), keychain)
