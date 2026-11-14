#!/usr/bin/env python3
"""Bring a pack (the skills, commands and subagents one type of project needs) from the hub into a project.

    pull.py packs   [--hub DIR]                    the packs people can choose from
    pull.py apply   PACK PROJECT [--hub DIR] [--force]
    pull.py check   PROJECT [--hub DIR]            what is out of date: newer in the hub, changed here, gone
    pull.py doctor  PROJECT [--lang zh-TW]         will usage be counted and shared? each problem with its fix

--hub is a checkout of the hub repository (default: the one this script sits in). Every command prints JSON,
so the project platform (or anything else) can show it; the files it writes are the interface:

  PROJECT/.claude/skills/<name>/, commands/<name>.md, agents/<name>.md   what Claude Code reads
  PROJECT/.claude/skills.lock.json      which pack, which hub commit, and the version of every item
  PROJECT/.claude/opencolony/            the counting hooks, and PROJECT/.claude/settings.local.json pointing at
                                         them (with this Python's full path), so usage is counted without a plugin

A file someone changed in the project is never overwritten (unless --force): it is reported instead.
Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import office            # beside this file in the plugin and in a hub's hub/ folder
except ImportError:          # run entirely on its own
    office = None

KINDS = {"skills": "skill", "commands": "command", "agents": "agent"}
TRACKER = "opencolony-skills"
HOOK_EVENTS = {"SessionStart": None, "UserPromptSubmit": None, "PostToolUse": "Skill|Agent|Task", "Stop": None, "SessionEnd": None}
MARK = ".claude/opencolony/record.py"
MIN_PYTHON = (3, 8)


def have(cmd: str) -> bool:
    return office.have(cmd) if office else bool(shutil.which(cmd))


def find_hub(start: Path) -> Path:
    for d in [start, *start.parents]:
        if (d / "hub.json").is_file() and (d / ".claude-plugin" / "marketplace.json").is_file():
            return d
    raise SystemExit("not inside a hub repository; give --hub")


def tree(p: Path) -> str:
    """One hash over a file, or over every file under a folder (names and contents)."""
    h = hashlib.sha256()
    files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file() and "__pycache__" not in f.parts)
    for f in files:
        h.update((f.relative_to(p).as_posix() if p.is_dir() else p.name).encode() + b"\0")
        h.update(f.read_bytes() + b"\0")
    return h.hexdigest()[:12] if files else ""


def main_file(kind: str, p: Path) -> Path:
    return p / "SKILL.md" if kind == "skill" else p


def version(kind: str, p: Path) -> str:
    try:
        return hashlib.sha256(main_file(kind, p).read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def git(hub: Path, *a) -> str:
    try:
        return subprocess.run(["git", *a], cwd=hub, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                              stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ------------------------------------------------------------------ packs

def packs(hub: Path) -> list[dict]:
    out = []
    for f in sorted((hub / "packs").glob("*.json")):
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as e:
            out.append({"name": f.stem, "error": f"not valid JSON: {e}"})
            continue
        items, missing = resolve(hub, p)
        out.append({"name": f.stem, "title": p.get("title") or f.stem, "description": p.get("description", ""),
                    "needs": p.get("needs") or [], "wants": p.get("wants") or [],
                    "items": [{"type": i["type"], "name": i["name"]} for i in items], "missing": missing})
    return out


def resolve(hub: Path, pack: dict) -> tuple[list[dict], list[str]]:
    """The hub folder or file of every item a pack names; names it cannot find are returned as missing."""
    items, missing = [], []
    for field, kind in KINDS.items():
        for name in pack.get(field) or []:
            pattern = {"skill": f"plugins/*/skills/{name}/SKILL.md", "command": f"plugins/*/commands/{name}.md",
                       "agent": f"plugins/*/agents/{name}.md"}[kind]
            found = [f for f in sorted(hub.glob(pattern)) if f.parts[-5 if kind == "skill" else -3] != TRACKER]
            if not found:
                missing.append(f"{kind} {name}")
                continue
            src = found[0].parent if kind == "skill" else found[0]
            to = {"skill": f".claude/skills/{name}", "command": f".claude/commands/{name}.md", "agent": f".claude/agents/{name}.md"}[kind]
            items.append({"type": kind, "name": name, "from": src.relative_to(hub).as_posix(), "to": to})
    return items, missing


def hub_url(hub: Path) -> str:
    """Where the hub is shared from: its origin (a relative local path made absolute), else the checkout itself."""
    url = git(hub, "remote", "get-url", "origin")
    if url and "://" not in url and not __import__("re").match(r"^[^/\\]+@[^:]+:", url) and (hub / url).exists():
        url = str((hub / url).resolve())
    return url or str(hub)


def hub_setting(hub: Path, key: str, default: str = "") -> str:
    try:
        return json.loads((hub / "hub.json").read_text(encoding="utf-8")).get(key) or default
    except (OSError, ValueError):
        return default


def hub_lang(hub: Path) -> str:
    return hub_setting(hub, "lang", "en")


def read_lock(project: Path) -> dict:
    f = project / ".claude" / "skills.lock.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    except ValueError:
        return {}


# ------------------------------------------------------------------ apply

def place(src: Path, dest: Path) -> None:
    if dest.is_dir():
        shutil.rmtree(dest)
    elif dest.exists():
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    else:
        shutil.copy2(src, dest)


def apply(hub: Path, name: str, project: Path, force: bool = False) -> dict:
    f = hub / "packs" / f"{name}.json"
    if not f.is_file():
        raise SystemExit(f"no pack {name!r} in {hub / 'packs'}")
    pack = json.loads(f.read_text(encoding="utf-8"))
    items, missing = resolve(hub, pack)
    old = read_lock(project)
    before = {(i["type"], i["name"]): i for i in old.get("items", [])}
    report = {"pack": name, "added": [], "updated": [], "unchanged": [], "removed": [], "kept_changes_here": [],
              "missing_in_hub": missing}
    entries = []
    for it in items:
        src, dest, k = hub / it["from"], project / it["to"], (it["type"], it["name"])
        label = f"{it['type']} {it['name']}"
        prev = before.pop(k, None)
        here = tree(dest) if dest.exists() else ""
        changed_here = bool(here) and (prev is None or here != prev.get("tree"))
        if changed_here and not force:
            report["kept_changes_here"].append(label)
            if prev:
                entries.append(prev)
            continue
        new_tree = tree(src)
        if prev and here == new_tree:
            report["unchanged"].append(label)
        else:
            place(src, dest)
            report["updated" if prev else "added"].append(label)
        entries.append({**it, "version": version(it["type"], dest), "tree": new_tree})
    for k, prev in before.items():          # in the old pack, not in this one
        dest = project / prev["to"]
        if dest.exists() and tree(dest) != prev.get("tree") and not force:
            report["kept_changes_here"].append(f"{k[0]} {k[1]}")
            entries.append(prev)            # still listed, so `check` says "gone from the pack"
            continue
        if dest.is_dir():
            shutil.rmtree(dest)
        elif dest.exists():
            dest.unlink()
        report["removed"].append(f"{k[0]} {k[1]}")
    lock = {"schema": 1, "hub": hub_url(hub), "hub_commit": git(hub, "rev-parse", "HEAD"),
            "pack": name, "lang": hub_lang(hub), "feedback": hub_setting(hub, "feedback"),
            "needs": pack.get("needs") or [], "wants": pack.get("wants") or [], "usage": hub_setting(hub, "usage", "git"), "pulled_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "items": sorted(entries, key=lambda e: (e["type"], e["name"]))}
    lock["tracking"] = install_tracking(hub, project)
    (project / ".claude" / "skills.lock.json").write_text(json.dumps(lock, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return report


def hook_command(project: Path, event: str) -> str:
    """The Python that runs pull.py (the project platform's) runs the hooks too, by its full path: no reliance on
    `python3` being on PATH, which on Windows it usually is not. Forward slashes work in every shell."""
    py = Path(sys.executable).resolve().as_posix()
    return f'"{py}" "{project.resolve().as_posix()}/{MARK}" {event}'


def install_tracking(hub: Path, project: Path) -> str:
    """Copy the counting hooks into the project and point .claude/settings.local.json at them: that file is this
    person's and this machine's (full paths), and Claude Code keeps it out of git. Other settings and hooks are
    kept. Returns the version of the copied hooks, or "" when the hub has none."""
    src = hub / "plugins" / TRACKER / "scripts"
    if not src.is_dir():
        return ""
    dest = project / ".claude" / "opencolony"
    place(src, dest)
    for skill in sorted((hub / "plugins" / TRACKER / "skills").glob("*/SKILL.md")):   # skill-suggestion
        place(skill.parent, project / ".claude" / "skills" / skill.parent.name)
    # the commands (/skill-feedback, /save-as-case, /skill-eval, /send-suggestion): Claude Code's own prompt refuses a
    # slash command it does not know before any hook runs, so the files must be in the project as they are in the plugin
    for cmd in sorted((hub / "plugins" / TRACKER / "commands").glob("*.md")):
        place(cmd, project / ".claude" / "commands" / cmd.name)
    ignore = project / ".claude" / ".gitignore"
    lines = ignore.read_text(encoding="utf-8").splitlines() if ignore.is_file() else []
    want = [x for x in ("settings.local.json", "opencolony/__pycache__/") if x not in lines]
    if want:
        ignore.write_text("\n".join(lines + want) + "\n", encoding="utf-8")
    shared = project / ".claude" / "settings.json"          # earlier versions wrote the hooks here
    if shared.is_file():
        try:
            st = json.loads(shared.read_text(encoding="utf-8"))
            if strip_ours(st):
                shared.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except ValueError:
            pass
    f = project / ".claude" / "settings.local.json"
    try:
        settings = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    except ValueError:
        return tree(dest)               # never rewrite a settings file we cannot read
    strip_ours(settings)
    hooks = settings.setdefault("hooks", {})
    for event, matcher in HOOK_EVENTS.items():
        ours = {"hooks": [{"type": "command", "command": hook_command(project, event), "timeout": 20}]}
        if matcher:
            ours = {"matcher": matcher, **ours}
        hooks[event] = hooks.get(event, []) + [ours]
    f.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return tree(dest)


def strip_ours(settings: dict) -> bool:
    """Remove the hook entries pull.py wrote before; True when there were any."""
    hooks = settings.get("hooks") or {}
    found = False
    for event in list(hooks):
        keep = [g for g in hooks[event] if not any(MARK in (h.get("command") or "").replace("\\", "/") for h in g.get("hooks", []))]
        found |= len(keep) != len(hooks[event])
        if keep:
            hooks[event] = keep
        else:
            del hooks[event]
    if "hooks" in settings and not settings["hooks"]:
        del settings["hooks"]
    return found


# ------------------------------------------------------------------ doctor

DOCTOR = {
    "en": {"python": "Python {v} at {p}", "python_old": "Python {v} is too old; the hooks need {m} or newer",
           "git": "git {v}", "nogit": "git is not installed: usage cannot be shared", "email": "shares usage as {u}",
           "noemail": "git has no user.email: usage is shared under the computer's user name ({u})",
           "hub": "can reach the hub", "nohub": "cannot reach the hub {h}: check the network or git access",
           "claude": "Claude Code {v}", "noclaude": "Claude Code is not installed or not on PATH",
           "hooks": "counting hooks installed", "nohooks": "counting hooks are missing: run pull.py apply again",
           "badhooks": "the hooks point at a Python or a folder that no longer exists (moved?): run pull.py apply again",
           "trusted": "this folder is trusted in Claude Code",
           "untrusted": "the first time this folder is opened in Claude Code, choose “Yes, I trust this folder”; until then "
                        "nothing is counted",
           "seen": "hooks last ran {t}", "notseen": "the hooks have not run yet (normal before the first session)",
           "synced": "usage last shared for {d}", "notsynced": "no usage shared yet",
           "need": "{c} is installed (this pack's skills need it)", "noneed": "{c} is not installed, and this pack's skills need it",
           "want": "{c} is installed", "nowant": "{c} is not installed: evaluations will work, but without pictures of the slides"},
    "zh-TW": {"python": "Python {v}（{p}）", "python_old": "Python {v} 太舊，需要 {m} 以上",
              "git": "git {v}", "nogit": "沒有安裝 git：用量無法分享", "email": "以 {u} 的名義分享用量",
              "noemail": "git 沒有設定 user.email：會用電腦的使用者名稱（{u}）分享用量",
              "hub": "連得到 hub", "nohub": "連不到 hub {h}：請檢查網路或 git 權限",
              "claude": "Claude Code {v}", "noclaude": "沒有安裝 Claude Code，或不在 PATH 上",
              "hooks": "計數用的 hook 已安裝", "nohooks": "缺少計數用的 hook：請重新執行 pull.py apply",
              "badhooks": "hook 指向的 Python 或資料夾已不存在（搬過位置？）：請重新執行 pull.py apply",
              "trusted": "Claude Code 已信任這個資料夾",
              "untrusted": "第一次用 Claude Code 打開這個資料夾時，請選「Yes, I trust this folder」；在那之前不會記錄任何東西",
              "seen": "hook 最後執行於 {t}", "notseen": "hook 還沒執行過（第一次使用前是正常的）",
              "synced": "用量已分享到 {d}", "notsynced": "還沒分享過用量",
              "need": "已安裝 {c}（這個 pack 的技能需要它）", "noneed": "沒有安裝 {c}，而這個 pack 的技能需要它",
              "want": "已安裝 {c}", "nowant": "沒有安裝 {c}：評估仍可進行，但看不到投影片的圖片"},
}


def trusted(project: Path) -> bool:
    """Claude Code remembers trust per folder (or a parent folder) in its config file."""
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    for f in (claude / ".claude.json", Path.home() / ".claude.json"):
        try:
            projects = json.loads(f.read_text(encoding="utf-8")).get("projects") or {}
        except (OSError, ValueError):
            continue
        p = project.resolve()
        for d in [p, *p.parents]:
            for k in (str(d), d.as_posix()):
                if (projects.get(k) or {}).get("hasTrustDialogAccepted"):
                    return True
    return False


def doctor(project: Path, lang: str = "") -> dict:
    """Everything that must hold for this project's usage to be counted and shared, each with a plain fix."""
    project = project.resolve()
    lock = read_lock(project)
    T = DOCTOR.get(lang or lock.get("lang") or "en", DOCTOR["en"])
    checks = []

    def add(name, ok, text):
        checks.append({"name": name, "ok": ok, "say": text})
    v = ".".join(map(str, sys.version_info[:3]))
    add("python", sys.version_info >= MIN_PYTHON, (T["python"] if sys.version_info >= MIN_PYTHON else T["python_old"])
        .format(v=v, p=Path(sys.executable).as_posix(), m=".".join(map(str, MIN_PYTHON))))
    gv = git(project, "--version")
    add("git", bool(gv), T["git"].format(v=gv.replace("git version ", "")) if gv else T["nogit"])
    email = git(project, "config", "user.email")
    sys.path.insert(0, str(project / ".claude" / "opencolony"))
    try:
        import common
        handle = common.user_handle()
        home = common.home()
    except ImportError:
        handle, home = "", Path.home() / ".opencolony" / "skills"
    add("name", bool(email), T["email"].format(u=handle) if email else T["noemail"].format(u=handle))
    hub = lock.get("hub", "")
    if hub and gv:
        reach = subprocess.run(["git", "ls-remote", "-q", "--exit-code", hub, "HEAD"], capture_output=True, timeout=30,
                               env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}).returncode == 0
        add("hub", reach, T["hub"] if reach else T["nohub"].format(h=hub))
    cv = ""
    if shutil.which("claude"):
        try:
            cv = subprocess.run(["claude", "--version"], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=30).stdout.strip().split(" ")[0]
        except (OSError, subprocess.TimeoutExpired):
            cv = "?"
    add("claude", bool(cv), T["claude"].format(v=cv) if cv else T["noclaude"])
    try:
        st = json.loads((project / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    cmds = [h.get("command", "") for gs in (st.get("hooks") or {}).values() for g in gs for h in g.get("hooks", [])
            if MARK in h.get("command", "")]
    paths_ok = all(all(Path(x).exists() for x in c.split('"')[1::2]) for c in cmds)
    add("hooks", bool(cmds) and paths_ok, T["hooks"] if cmds and paths_ok else T["badhooks"] if cmds else T["nohooks"])
    add("trust", trusted(project), T["trusted"] if trusted(project) else T["untrusted"])
    beat = home / "heartbeat"
    when = dt.datetime.fromtimestamp(int(beat.read_text())).astimezone().isoformat(timespec="minutes") if beat.is_file() else ""
    add("seen", bool(when), T["seen"].format(t=when) if when else T["notseen"])
    try:
        days = json.loads((home / "synced.json").read_text())
    except (OSError, ValueError):
        days = []
    if lock.get("usage") != "none":                # a public hub collects no usage: nothing to be shared
        add("shared", bool(days), T["synced"].format(d=max(days)) if days else T["notsynced"])
    must = {"python", "git", "hooks", "trust"}
    for c in lock.get("needs") or []:
        found = have(c)
        add(f"need:{c}", found, T["need" if found else "noneed"].format(c=c))
        must.add(f"need:{c}")
    for c in lock.get("wants") or []:
        found = have(c)
        add(f"want:{c}", found, T["want" if found else "nowant"].format(c=c))
    return {"ok": all(c["ok"] for c in checks if c["name"] in must), "checks": checks}


# ------------------------------------------------------------------ check

def check(hub: Path, project: Path) -> dict:
    lock = read_lock(project)
    if not lock:
        return {"pack": None, "out_of_date": False, "items": [], "note": "no pack was pulled into this project"}
    f = hub / "packs" / f"{lock['pack']}.json"
    items, missing = resolve(hub, json.loads(f.read_text(encoding="utf-8"))) if f.is_file() else ([], [])
    now = {(i["type"], i["name"]): i for i in items}
    out = []
    for e in lock.get("items", []):
        k = (e["type"], e["name"])
        dest = project / e["to"]
        here = tree(dest) if dest.exists() else ""
        hub_tree = tree(hub / now[k]["from"]) if k in now else ""
        mine = here != e.get("tree")
        theirs = k in now and hub_tree != e.get("tree")
        state = ("gone from the pack" if k not in now else "both changed" if mine and theirs else
                 "newer in the hub" if theirs else "changed here" if mine else "up to date")
        out.append({"type": e["type"], "name": e["name"], "state": state})
    for k in now.keys() - {(e["type"], e["name"]) for e in lock.get("items", [])}:
        out.append({"type": k[0], "name": k[1], "state": "added to the pack"})
    tracker = hub / "plugins" / TRACKER / "scripts"
    return {"pack": lock["pack"], "hub_commit": lock.get("hub_commit"), "hub_now": git(hub, "rev-parse", "HEAD"),
            "out_of_date": any(i["state"] in ("newer in the hub", "both changed", "gone from the pack", "added to the pack") for i in out)
            or (tracker.is_dir() and tree(tracker) != lock.get("tracking")),
            "items": sorted(out, key=lambda i: (i["type"], i["name"])), "missing_in_hub": missing}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hub", type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("packs")
    a = sub.add_parser("apply"); a.add_argument("pack"); a.add_argument("project", type=Path); a.add_argument("--force", action="store_true")
    c = sub.add_parser("check"); c.add_argument("project", type=Path)
    d = sub.add_parser("doctor"); d.add_argument("project", type=Path); d.add_argument("--lang", default="")
    args = ap.parse_args(argv)
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(f"Python {'.'.join(map(str, MIN_PYTHON))} or newer is needed")
    hub = None if args.cmd == "doctor" else (args.hub or find_hub(Path(__file__).resolve().parent)).resolve()
    if args.cmd == "packs":
        out = packs(hub)
    elif args.cmd == "apply":
        args.project.mkdir(parents=True, exist_ok=True)
        out = apply(hub, args.pack, args.project.resolve(), args.force)
    elif args.cmd == "doctor":
        out = doctor(args.project, args.lang)
    else:
        out = check(hub, args.project.resolve())
    sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
