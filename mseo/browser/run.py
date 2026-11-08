"""Run one `claude -p` agent in a workspace with the sealed pull settings.

The agent drives Chrome through the named Chrome tools and saves files to <ws>/data/inbox/; it writes no record.
The run uses the operator's Claude subscription: API keys are removed from its environment, and a run that reports
any other auth source is `auth_failed`. Usage is recorded as tokens, turns and minutes, never as money.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from mseo import seal

TIMEOUT_S = 45 * 60
MAX_TURNS = 80  # a live Ads pull used 52; the token budget is the real cap
MODEL = os.environ.get("MSEO_AGENT_MODEL", "sonnet")
PROMPTS = Path(__file__).resolve().parent / "prompts"
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
SUBSCRIPTION = "none"  # apiKeySource when claude -p runs on the subscription login


@dataclass
class AgentResult:
    status: str  # ok | auth_failed | error
    error: str | None
    usage: dict | None
    exit_code: int | None
    denials: list = field(default_factory=list)
    reply: str = ""  # the agent's final message, e.g. "saved" or "failed: <reason>"
    calls: list = field(default_factory=list)  # every Chrome tool call in the stream: {name, input, text}

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def subprocess_runner(argv: list[str], *, cwd: Path, env: dict, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, env=env, timeout=timeout, capture_output=True, text=True, check=False,
                          stdin=subprocess.DEVNULL)


SELECT_DEVICE = ("Before anything else, call mcp__claude-in-chrome__list_connected_browsers, then "
                 "mcp__claude-in-chrome__select_browser with deviceId {device}. If no browser with that deviceId is "
                 "listed, stop and reply `failed: browser not connected`. Never select any other browser.")
NO_DEVICE = ("If Claude in Chrome reports that several browsers are connected and none is selected, do not choose one: "
             "stop and reply `failed: no browser configured`.")


ONE_TAB = ("Work in one tab only: the tab you open for this task. Never switch to, read or act in any other tab, and "
           "open no second tab; a run that uses two tabs is refused.")


def prompt(name: str, ws, *, source: str, **values: str) -> str:
    """A generic prompt from prompts/<name>.md, filled from config values (no customer text lives in the file),
    led by the instruction that selects the identity's browser."""
    device = ws.identity_for(source).get("chrome_device")
    lead = SELECT_DEVICE.format(device=device) if device else NO_DEVICE
    body = (PROMPTS / f"{name}.md").read_text(encoding="utf-8").format(slug=ws.slug, **values)
    return f"{lead}\n\n{ONE_TAB}\n\n{body}"


def _events(stdout: str) -> list[dict]:
    out = []
    for line in stdout.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


CHROME = "mcp__claude-in-chrome__"


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def chrome_calls(events: list[dict]) -> list[dict]:
    """Every Chrome tool call in the harness's own record, in order: {name, input, text}. `text` is the tool_result
    (None for an error or a missing result). The agent's own text and other tools' results are never included."""
    calls, by_id = [], {}
    for e in events:
        message = e.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if e.get("type") == "assistant" and block.get("type") == "tool_use":
                if str(block.get("name") or "").startswith(CHROME):
                    c = {"name": block["name"], "input": block.get("input") if isinstance(block.get("input"), dict)
                         else {}, "text": None}
                    calls.append(c)
                    by_id[block.get("id")] = c
            elif e.get("type") == "user" and block.get("type") == "tool_result":
                c = by_id.pop(block.get("tool_use_id"), None)  # one result per call; a repeated id is ignored
                if c is not None and not block.get("is_error"):
                    c["text"] = _text(block.get("content"))
    return calls


def _usage(result: dict, model: str | None) -> dict | None:
    u = result.get("usage")
    if not isinstance(u, dict):
        return None
    models = list((result.get("modelUsage") or {}).keys())
    return {"tokens_in": int(u.get("input_tokens") or 0), "tokens_out": int(u.get("output_tokens") or 0),
            "cache_read": int(u.get("cache_read_input_tokens") or 0),
            "cache_write": int(u.get("cache_creation_input_tokens") or 0),
            "turns": int(result.get("num_turns") or 0), "minutes": round((result.get("duration_ms") or 0) / 60000, 2),
            "model": model or (models[0] if models else "unknown")}


def run_agent(ws, text: str, *, runner: Callable = subprocess_runner, model: str = MODEL,
              require_seal: bool = True) -> AgentResult:
    if require_seal:
        seal.require_pull_ready(ws)
    argv = ["claude", "-p", text, *seal.pull_flags(ws), "--max-turns", str(MAX_TURNS), "--no-session-persistence",
            "--output-format", "stream-json", "--verbose", "--model", model]
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
    env.update(MSEO_WORKSPACES=str(ws.root.parent), PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = runner(argv, cwd=ws.root, env=env, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return AgentResult("error", f"agent timed out after {TIMEOUT_S // 60} minutes", None, None)
    events = _events(proc.stdout or "")
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    usage = _usage(result, init.get("model"))
    denials = result.get("permission_denials") or []
    source = init.get("apiKeySource")
    if source != SUBSCRIPTION:
        return AgentResult("auth_failed", f"agent did not run on the subscription login (apiKeySource={source!r})",
                           usage, proc.returncode, denials)
    if proc.returncode != 0 or result.get("is_error"):
        why = (proc.stderr or str(result.get("result") or ""))[-300:]
        return AgentResult("error", f"agent exited {proc.returncode}: {why}".strip(), usage, proc.returncode, denials)
    return AgentResult("ok", None, usage, proc.returncode, denials, str(result.get("result") or "").strip(),
                       calls=chrome_calls(events))
