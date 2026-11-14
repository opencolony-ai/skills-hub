#!/usr/bin/env python3
"""Keep a project's skills current: when a session starts, see whether the hub has moved and, if it has, bring the
project's pack up to date and say what changed, with the note the owner wrote when they released it.

    update.py auto [--cwd DIR]        what the SessionStart hook runs (prints the message, or nothing)
    update.py now  [--cwd DIR]        the same, without the "checked a moment ago" shortcut; prints JSON

Rules: it never waits long (a slow or missing network means "not this time"); a file someone changed in the project
is left alone and named; nothing is sent anywhere. Claude Code reads a skill's text when the skill is used, so an
update made as the session starts is the version that session uses.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import pull  # noqa: E402

EVERY = 600                 # seconds between looks at the hub, per project
SAY = {
    "en": {"updated": "Skills updated from the hub: {x}.", "added": "New: {x}.", "removed": "Removed: {x}.",
           "kept": "Not touched because they were changed in this project: {x}.", "note": "{name}: {note}",
           "evidence": " (own cases: {a} of {n} pass, was {b} of {m})"},
    "zh-TW": {"updated": "技能已從 hub 更新：{x}。", "added": "新增：{x}。", "removed": "移除：{x}。",
              "kept": "這些在專案裡被改過，所以沒有動：{x}。", "note": "{name}：{note}",
              "evidence": "（考題 {n} 題過 {a} 題，原本 {m} 題過 {b} 題）"},
}


def git(args: list[str], cwd: Path | None = None, timeout: int = 10) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def releases(hub: Path) -> list[dict]:
    out = []
    try:
        for line in (hub / "releases.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def refresh(hub_url: str, budget: int = 12) -> Path | None:
    """The hub's copy on this computer, brought to what the hub has now; None when that cannot be done in time."""
    clone = common.home() / "hub"
    if not (clone / ".git").is_dir():
        clone.parent.mkdir(parents=True, exist_ok=True)
        if git(["clone", "-q", "--filter=blob:none", hub_url, str(clone)], timeout=budget * 3)[0] != 0:
            return None
        return clone
    code, remote = git(["ls-remote", hub_url, "HEAD"], timeout=5)
    if code != 0 or not remote:
        return None
    if remote.split()[0] == git(["rev-parse", "HEAD"], cwd=clone)[1]:
        return clone
    if git(["fetch", "-q", "origin"], cwd=clone, timeout=budget)[0] != 0:
        return None
    head = git(["rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=clone)[1] or "origin/main"
    git(["checkout", "-q", "-B", head.split("/", 1)[-1], head], cwd=clone)
    git(["reset", "-q", "--hard", head], cwd=clone)
    return clone


def run(cwd: str, force: bool = False) -> dict:
    """{"changed": bool, "said": message, "report": pull's report} for the project that holds cwd."""
    root = common.project_root(cwd)
    lock = pull.read_lock(root)
    hub_url = lock.get("hub") or ""
    if not lock.get("pack") or not hub_url:
        return {"changed": False, "said": "", "why": "no pack was pulled into this project"}
    stamp = common.home() / "updates" / f"{common.project_id(str(root))}.json"
    if not force and stamp.is_file() and time.time() - stamp.stat().st_mtime < EVERY:
        return {"changed": False, "said": "", "why": "checked a moment ago"}
    clone = refresh(hub_url)
    if clone is None:
        return {"changed": False, "said": "", "why": "the hub could not be reached in time"}
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps({"at": time.time()}), encoding="utf-8")
    if not (clone / "packs" / f"{lock['pack']}.json").is_file():
        return {"changed": False, "said": "", "why": f"the hub no longer has the pack {lock['pack']}"}
    before = {(i["type"], i["name"]): i.get("version", "") for i in lock.get("items", [])}
    report = pull.apply(clone, lock["pack"], root)
    T = SAY.get(lock.get("lang") or pull.hub_lang(clone), SAY["en"])
    names = lambda xs: ", ".join(x.split(" ", 1)[1] for x in xs)        # "skill pptx" -> "pptx"
    parts = []
    if report["updated"]:
        parts.append(T["updated"].format(x=names(report["updated"])))
        notes = releases(clone)
        after = {(i["type"], i["name"]): i.get("version", "") for i in pull.read_lock(root).get("items", [])}
        for label in report["updated"]:
            kind, name = label.split(" ", 1)
            mine = [r for r in notes if r.get("skill", "").lstrip("/@") == name and r.get("version") == after.get((kind, name))]
            if mine and mine[-1].get("note"):
                r = mine[-1]
                ev = r.get("evidence") or {}
                parts.append(T["note"].format(name=name, note=r["note"]) + (T["evidence"].format(
                    a=ev["passed"][0], n=ev["passed"][1], b=ev["passed_before"][0], m=ev["passed_before"][1])
                    if ev.get("passed") and ev.get("passed_before") else ""))
    if report["added"] and before:
        parts.append(T["added"].format(x=names(report["added"])))
    if report["removed"]:
        parts.append(T["removed"].format(x=names(report["removed"])))
    if report["kept_changes_here"]:
        parts.append(T["kept"].format(x=names(report["kept_changes_here"])))
    changed = bool(report["updated"] or report["removed"] or (report["added"] and before))
    if changed:
        common.append({"kind": "update", "pack": lock["pack"], "updated": report["updated"], "added": report["added"],
                       "removed": report["removed"]})
    return {"changed": changed, "said": " ".join(parts) if changed else "", "report": report}     # quiet when nothing moved


def main(argv=None) -> None:
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a[0] if a else "auto"
    cwd = a[a.index("--cwd") + 1] if "--cwd" in a and a.index("--cwd") + 1 < len(a) else os.getcwd()
    try:
        out = run(cwd, force=cmd == "now")
    except SystemExit as e:
        out = {"changed": False, "said": "", "why": str(e)}
    if cmd == "now":
        common.write_stdout_json(out)
    elif out.get("said"):
        print(out["said"])


if __name__ == "__main__":
    main()
