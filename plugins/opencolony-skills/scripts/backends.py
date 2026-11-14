"""Model backends: the command-line agents the platform drives, each with the person's own login.

    claude    Anthropic's Claude Code, the unmodified `claude` program on this computer
    copilot   GitHub Copilot CLI, the `copilot` program on this computer

Rules that hold for every backend (they are what keeps this on the right side of each vendor's terms):
  - the program is launched as it is installed: never modified, never given another program's credentials;
  - this code never reads, stores or forwards a login: the person signs in to the program themselves;
  - the number of runs at once is capped by the caller (jobs.py), because usage draws from the person's own plan.

A headless run returns {answer, tokens, seconds, used, error, skills_known}: `used` lists the skills and subagents
the run called, `skills_known` is False when the backend's output does not say (then `used` is not evidence).
Standard library only.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import common

NAMES = ("claude", "copilot")
LABEL = {"claude": "Claude Code", "copilot": "GitHub Copilot CLI"}


def _kwargs() -> dict:
    return {} if os.name == "nt" else {"start_new_session": True}


def find(name: str) -> str:
    """The program's path: OPENCOLONY_<NAME>_BIN, else on PATH, else "" ."""
    return os.environ.get(f"OPENCOLONY_{name.upper()}_BIN") or shutil.which(name) or ""


def info(name: str) -> dict:
    path = find(name)
    version = ""
    if path:
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=20, stdin=subprocess.DEVNULL).stdout.strip().splitlines()
            version = out[0][:80] if out else ""
        except (OSError, subprocess.TimeoutExpired):
            version = "?"
    return {"name": name, "label": LABEL[name], "found": bool(path), "path": path, "version": version}


def chosen() -> str:
    """Which backend runs things: OPENCOLONY_BACKEND, else "backend" in config.json, else the first one installed."""
    want = os.environ.get("OPENCOLONY_BACKEND") or common.config().get("backend") or ""
    if want in NAMES:
        return want
    for n in NAMES:
        if find(n):
            return n
    return "claude"


def get(name: str = "") -> "Backend":
    return {"claude": Claude, "copilot": Copilot}[name or chosen()]()


def run_process(cmd: list[str], cwd: Path | str, timeout: int, cancel: threading.Event | None = None,
                env: dict | None = None) -> tuple[str, str, int, bool]:
    """(stdout, stderr, exit code, stopped): runs until it ends, the time is up, or `cancel` is set."""
    p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace", env=env, **_kwargs())
    t0, stopped = time.time(), False
    while True:
        try:
            out, err = p.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if (cancel and cancel.is_set()) or time.time() - t0 > timeout:
                stopped = True
                try:
                    if os.name == "nt":
                        p.kill()
                    else:
                        os.killpg(p.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
                out, err = p.communicate()
                break
    return out, err, p.returncode if p.returncode is not None else -1, stopped


class Backend:
    name = ""

    def __init__(self) -> None:
        self.path = find(self.name)

    def interactive(self) -> list[str]:
        """The command for a terminal the person works in."""
        return [self.path or self.name]

    def headless(self, prompt: str, cwd: Path | str, tools: list[str] | None = None, max_turns: int | None = None,
                 timeout: int = 900, cancel: threading.Event | None = None) -> dict:
        raise NotImplementedError

    def test(self) -> dict:
        """A tiny real run, started by the person, to see that it answers: costs a little of their plan."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            r = self.headless("Reply with the single word OK.", d, max_turns=1, timeout=120)
        return {"ok": bool(r["answer"]) and not r["error"], "answer": r["answer"][:80], "error": r["error"],
                "tokens": r["tokens"], "seconds": r["seconds"]}


def _blank() -> dict:
    return {"answer": "", "tokens": 0, "seconds": 0.0, "used": [], "error": "", "skills_known": True}


class Claude(Backend):
    name = "claude"

    def headless(self, prompt, cwd, tools=None, max_turns=None, timeout=900, cancel=None) -> dict:
        res = _blank()
        if not self.path:
            return {**res, "error": "Claude Code is not installed (the `claude` program was not found)"}
        cmd = [self.path, "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--setting-sources", "project,local", "--strict-mcp-config", "--permission-mode", "acceptEdits",
               "--disallowedTools", "WebFetch", "WebSearch"]
        if tools:
            cmd += ["--allowedTools", *tools]
        if max_turns:
            cmd += ["--max-turns", str(max_turns)]
        t0 = time.time()
        try:
            out, err, _, stopped = run_process(cmd, cwd, timeout, cancel)
        except OSError as e:
            return {**res, "error": f"cannot start Claude Code: {e}"}
        res["seconds"] = round(time.time() - t0, 1)
        for line in out.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "assistant":
                for c in (e.get("message") or {}).get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        inp = c.get("input") or {}
                        if c.get("name") == "Skill" and inp.get("skill"):
                            res["used"].append(inp["skill"])
                        elif c.get("name") in ("Agent", "Task") and inp.get("subagent_type"):
                            res["used"].append("@" + inp["subagent_type"])
            elif e.get("type") == "result":
                res["answer"] = e.get("result") or ""
                u = e.get("usage") or {}
                res["tokens"] = sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens",
                                                                  "cache_creation_input_tokens", "cache_read_input_tokens"))
                if e.get("duration_ms"):
                    res["seconds"] = round(e["duration_ms"] / 1000, 1)
                if e.get("is_error"):
                    res["error"] = str(e.get("result") or "error")[:300]
        if stopped and not res["answer"]:
            res["error"] = "stopped"
        elif not out.strip():
            res["error"] = (err or "no output").strip()[:300]
        return res


class Copilot(Backend):
    """GitHub Copilot CLI. Written from GitHub's documentation (`copilot -p`, `--output-format=json`, `--allow-tool`,
    `--no-ask-user`); the JSON lines are read leniently because their fields are not documented. Not yet tried against
    a real Copilot CLI."""
    name = "copilot"

    def headless(self, prompt, cwd, tools=None, max_turns=None, timeout=900, cancel=None) -> dict:
        res = {**_blank(), "skills_known": False}
        if not self.path:
            return {**res, "error": "GitHub Copilot CLI is not installed (the `copilot` program was not found)"}
        cmd = [self.path, "-p", prompt, "--output-format=json", "--no-ask-user"]
        for t in tools or []:
            if t == "Bash":
                cmd.append("--allow-tool=shell")
            elif t in ("Write", "Edit", "NotebookEdit"):
                cmd.append("--allow-tool=write")
        t0 = time.time()
        try:
            out, err, code, stopped = run_process(cmd, cwd, timeout, cancel)
        except OSError as e:
            return {**res, "error": f"cannot start GitHub Copilot CLI: {e}"}
        res["seconds"] = round(time.time() - t0, 1)
        res["answer"], res["tokens"] = parse_copilot(out)
        if stopped and not res["answer"]:
            res["error"] = "stopped"
        elif code != 0 and not res["answer"]:
            res["error"] = (err or out or "no output").strip()[:300]
        return res


def parse_copilot(out: str) -> tuple[str, int]:
    """(answer, tokens) from Copilot CLI output: JSON lines when it printed them, else the plain text."""
    events = []
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            events.append(e)
    if not events:
        return out.strip(), 0
    answer, tokens = "", 0

    def walk(x, depth=0):
        nonlocal tokens
        if depth > 6:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(v, (int, float)) and "token" in k.lower():
                    tokens = max(tokens, int(v)) if k.lower() in ("total_tokens", "totaltokens") else tokens
                walk(v, depth + 1)
        elif isinstance(x, list):
            for v in x:
                walk(v, depth + 1)
    for e in events:
        kind = str(e.get("type", "")).lower()
        if any(w in kind for w in ("assistant", "message", "result", "final")):
            for k in ("result", "content", "text", "message"):
                v = e.get(k) or (e.get("data") or {}).get(k) if isinstance(e.get("data"), dict) or e.get(k) else None
                if isinstance(v, str) and v.strip():
                    answer = v.strip()
        walk(e)
    if not tokens:                       # parts, when no total is given
        for e in events:
            u = e.get("usage") or (e.get("data") or {}).get("usage") if isinstance(e.get("data"), dict) or e.get("usage") else None
            if isinstance(u, dict):
                tokens += sum(int(v) for k, v in u.items() if isinstance(v, (int, float)) and "token" in k.lower())
    return answer or out.strip(), tokens
