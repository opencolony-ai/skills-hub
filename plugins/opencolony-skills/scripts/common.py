"""Shared by the plugin's scripts: where the local record lives, and how a day of events becomes the summary
shared with the hub. Standard library only; nothing here ever blocks or fails a Claude Code session."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path

SCHEMA = 1
OWN_SKILLS = {"skill-suggestion"}     # the plugin's own skill: not one of the company's skills to count


def home() -> Path:
    """~/.opencolony/skills, or OPENCOLONY_SKILLS_HOME."""
    return Path(os.environ.get("OPENCOLONY_SKILLS_HOME") or Path.home() / ".opencolony" / "skills")


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def config() -> dict:
    f = home() / "config.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    except (OSError, ValueError):
        return {}


def save_config(updates: dict) -> dict:
    """Write keys into config.json (a None value removes the key); returns the whole config."""
    cfg = config()
    for k, v in updates.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    home().mkdir(parents=True, exist_ok=True)
    tmp = home() / "config.json.tmp"
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, home() / "config.json")
    return cfg


def today(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now()).strftime("%Y-%m-%d")


def append(event: dict, now: dt.datetime | None = None) -> None:
    """One event, one line, in events/<local date>.jsonl."""
    now = now or dt.datetime.now()
    d = home() / "events"
    d.mkdir(parents=True, exist_ok=True)
    event = {"at": now.astimezone().isoformat(timespec="seconds"), **event}
    with open(d / f"{today(now)}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def state(session: str) -> dict:
    f = home() / "sessions" / f"{session}.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    except (OSError, ValueError):
        return {}


def save_state(session: str, s: dict) -> None:
    d = home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session}.json").write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------ which skill, command or subagent, which version

# what Claude Code reads for each kind, relative to a .claude-like folder or a plugin
FILES = {"skill": "skills/{n}/SKILL.md", "command": "commands/{n}.md", "agent": "agents/{n}.md"}
MARK = {"skill": "", "command": "/", "agent": "@"}       # how the summary and the hub name them


def key(kind: str, name: str) -> str:
    """The name usage and the inventory share: weekly-report, /team-skills:standup, @reviewer."""
    return MARK.get(kind, "") + name


def find_item(kind: str, name: str, cwd: str) -> tuple[str, str, Path | None]:
    """(version, source, file) of a skill, command or subagent: the version is the first 12 hex digits of
    SHA-256 over its main file; the source is project, personal or plugin:<name>. ("", "unknown", None) when
    no such file exists (a built-in command or subagent, or a typo)."""
    plugin, _, short = name.rpartition(":")
    rel = FILES[kind].format(n=short.replace(":", "/"))
    plugins = claude_dir() / "plugins"
    if plugin:   # an installed plugin: plugins/cache/<marketplace>/<plugin>/<version>/..., or a marketplace folder
        found = sorted(plugins.glob(f"cache/*/{plugin}/*/{rel}"), key=lambda f: f.stat().st_mtime)
        found = found or [d / rel for d in marketplace_plugin_dirs(plugin) if (d / rel).is_file()]
        return (digest(found[-1]), f"plugin:{plugin}", found[-1]) if found else ("", f"plugin:{plugin}", None)
    candidates: list[tuple[Path, str]] = []
    p = Path(cwd or ".").resolve()
    for d in [p, *p.parents]:
        for base in (".claude", ".github", ".agents"):
            candidates.append((d / base / rel, "project"))
        if (d / ".git").exists():
            break
    for base in (claude_dir(), Path.home() / ".copilot", Path.home() / ".agents"):
        candidates.append((base / rel, "personal"))
    for f, source in candidates:
        if f.is_file():
            return digest(f), source, f
    return "", "unknown", None


def find_skill(name: str, cwd: str) -> tuple[str, str]:
    v, src, _ = find_item("skill", name, cwd)
    return v, src


def project_root(cwd: str) -> Path:
    """The project a session works in: the folder with .claude/skills.lock.json, else the git root, else cwd."""
    p = Path(cwd or ".").resolve()
    for d in [p, *p.parents]:
        if (d / ".claude" / "skills.lock.json").is_file():
            return d
        if (d / ".git").exists():
            return d
    return p


def project_id(cwd: str) -> str:
    """Names the project in local events only (never shared): a hash of its folder."""
    return hashlib.sha256(str(project_root(cwd)).encode()).hexdigest()[:12]


def lang(cwd: str = "") -> str:
    return project_lock(cwd).get("lang") or config().get("lang") or "en"


def user_handle() -> str:
    """The name usage is shared under: "user" in config.json, else the part of the git email before @."""
    import subprocess
    u = config().get("user") or ""
    if not u:
        try:
            u = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            u = ""
    u = u or os.environ.get("USER") or os.environ.get("USERNAME") or "someone"
    return u.split("@")[0].lower().replace(" ", "-")


# ------------------------------------------------------------------ where files and issues go

def onedrive_dir() -> Path | None:
    """The person's OneDrive (work) folder, which the OneDrive app keeps in sync: "onedrive" in config.json,
    else OneDriveCommercial / OneDrive (Windows), else ~/Library/CloudStorage/OneDrive-* (macOS)."""
    for v in (os.environ.get("OPENCOLONY_ONEDRIVE"), config().get("onedrive"), os.environ.get("OneDriveCommercial"),
              os.environ.get("OneDrive")):
        if v and Path(v).is_dir():
            return Path(v)
    cloud = Path.home() / "Library" / "CloudStorage"
    found = sorted(cloud.glob("OneDrive-*")) if cloud.is_dir() else []
    work = [f for f in found if f.name != "OneDrive-Personal"]
    return (work or found or [None])[0]


def parse_repo(spec: str, like: str = "") -> tuple[str, str, str]:
    """(host, owner, repo) from a git URL or owner/repo; owner/repo takes the host of `like` (the hub)."""
    import re as _re
    m = _re.match(r"^(?:https?://|ssh://)?(?:[^@/]+@)?([^/:]+)[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", spec or "")
    if m and ("." in m.group(1) or m.group(1) == "localhost"):
        return m.group(1), m.group(2), m.group(3)
    if _re.match(r"^[\w.-]+/[\w.-]+$", spec or ""):
        host = parse_repo(like)[0] if like else "github.com"
        return host or "github.com", *spec.split("/")
    return "", "", ""


def api_base(host: str) -> str:
    if os.environ.get("OPENCOLONY_GITHUB_API"):
        return os.environ["OPENCOLONY_GITHUB_API"].rstrip("/")
    return "https://api.github.com" if host in ("github.com", "www.github.com") else f"https://{host}/api/v3"


def github_token(host: str) -> str:
    """A token for the GitHub API from what the person already has: the gh CLI's login, else the HTTPS login
    git keeps in its credential helper (Git Credential Manager on Windows and macOS). Never asks."""
    import shutil
    import subprocess
    if os.environ.get("OPENCOLONY_GITHUB_TOKEN"):
        return os.environ["OPENCOLONY_GITHUB_TOKEN"]
    quiet = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GH_PROMPT_DISABLED": "1"}
    try:
        if shutil.which("gh"):
            t = subprocess.run(["gh", "auth", "token", "--hostname", host], capture_output=True, text=True,
                               timeout=15, env=quiet).stdout.strip()
            if t:
                return t
        out = subprocess.run(["git", "credential", "fill"], input=f"protocol=https\nhost={host}\n\n", capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=20, env=quiet).stdout
        for line in out.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def read_stdin_json() -> dict:
    """Hook input is UTF-8 whatever the console's code page (Windows consoles are often not)."""
    import sys
    return json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")


def write_stdout_json(obj: dict) -> None:
    import sys
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.flush()


def detach_kwargs() -> dict:
    """Start a background process that outlives the hook, on Windows as elsewhere."""
    import subprocess
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}


def project_lock(cwd: str) -> dict:
    """The project's .claude/skills.lock.json (written when a pack is pulled into it): its pack and hub."""
    p = Path(cwd or ".").resolve()
    for d in [p, *p.parents]:
        f = d / ".claude" / "skills.lock.json"
        if f.is_file():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
        if (d / ".git").exists():
            break
    return {}


def first_time(*parts: str, window: float = 0) -> bool:
    """True once per event: the plugin and a project's own hooks may both be installed, and must not count
    the same thing twice. With a window, the same event again after that many seconds counts again (the two
    copies of a hook run within a moment of each other; a person typing the same thing twice does not).
    Keeps only a hash."""
    import time
    d = home() / "seen"
    d.mkdir(parents=True, exist_ok=True)
    f = d / hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:24]
    for _ in range(2):
        try:
            os.close(os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if not window or time.time() - f.stat().st_mtime < window:
                    return False
                f.unlink()
            except OSError:
                return False
        except OSError:
            return True
    return False


def forget_old_events(days: int = 3) -> None:
    import time
    for f in (home() / "seen").glob("*"):
        try:
            if time.time() - f.stat().st_mtime > days * 86400:
                f.unlink()
        except OSError:
            pass


def marketplace_plugin_dirs(plugin: str) -> list[Path]:
    """Where a plugin lives inside the marketplaces Claude Code knows (a marketplace added from a folder runs its
    plugins from there, not from the cache)."""
    out = []
    try:
        known = json.loads((claude_dir() / "plugins" / "known_marketplaces.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for m in known.values():
        loc = Path((m or {}).get("installLocation") or "")
        try:
            listed = json.loads((loc / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")).get("plugins", [])
        except (OSError, ValueError):
            continue
        for p in listed:
            if p.get("name") == plugin and isinstance(p.get("source"), str):
                out.append((loc / p["source"]).resolve())
    return out


def digest(f: Path) -> str:
    try:
        return hashlib.sha256(f.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


# ------------------------------------------------------------------ a session's tokens, from its transcript

def transcript_summary(path: str) -> dict:
    """Tokens (input, output, cache write, cache read), models and the skills invoked, from the transcript
    Claude Code keeps for the session. Never reads or keeps the text of prompts or answers."""
    out = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "models": [], "skills": [], "turns": 0}
    models, skills = set(), []
    try:
        lines = open(path, encoding="utf-8")
    except (OSError, TypeError):
        return out
    with lines:
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            m = e.get("message")
            if not isinstance(m, dict):
                continue
            if e.get("type") == "user" and isinstance(m.get("content"), str):
                out["turns"] += 1
            u = m.get("usage")
            if isinstance(u, dict):
                out["input"] += u.get("input_tokens") or 0
                out["output"] += u.get("output_tokens") or 0
                out["cache_write"] += u.get("cache_creation_input_tokens") or 0
                out["cache_read"] += u.get("cache_read_input_tokens") or 0
                if m.get("model"):
                    models.add(m["model"])
            for c in m.get("content") if isinstance(m.get("content"), list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Skill":
                    s = (c.get("input") or {}).get("skill")
                    if s and s not in skills and not s.startswith("opencolony-skills:") and s.split(":")[-1] not in OWN_SKILLS:
                        skills.append(s)
    out["models"], out["skills"] = sorted(models), skills
    out["total"] = out["input"] + out["output"] + out["cache_write"] + out["cache_read"]
    return out


# ------------------------------------------------------------------ "no, that's wrong": the person corrects Claude

CORRECTION = re.compile(
    r"^\s*(no\b|nope\b|wrong\b|that'?s (not|wrong)|not (what|right)|undo\b|revert\b|try again\b|incorrect\b|"
    r"you (missed|forgot|broke)|不對|不是|錯了|錯誤|重來|再試|改回|你漏了)", re.I)


def is_correction(prompt: str) -> bool:
    return bool(CORRECTION.search(prompt or ""))


# ------------------------------------------------------------------ a day of events -> the summary shared with the hub

def week_of(date: str) -> str:
    """The ISO week a date belongs to: 2026-11-14 -> 2026-W46."""
    y, w, _ = dt.date.fromisoformat(date).isocalendar()
    return f"{y}-W{w:02d}"


def week_days(week: str) -> list[str]:
    y, w = week.split("-W")
    monday = dt.date.fromisocalendar(int(y), int(w), 1)
    return [(monday + dt.timedelta(days=i)).isoformat() for i in range(7)]


def summarise(events: list[dict], user: str, date: str, week: str = "", days: list[str] | None = None) -> dict:
    """What is shared: counts per skill, command and subagent (and per pack, the project type it was pulled
    with), tokens, feedback and the notes people typed for feedback. Never prompts, answers, file paths or
    repository names."""
    skills: dict[str, dict] = {}
    sessions: dict[str, dict] = {}
    packs: dict[str, set] = {}

    def sk(name: str) -> dict:
        kind = "command" if name.startswith("/") else "agent" if name.startswith("@") else "skill"
        return skills.setdefault(name, {"type": kind, "uses": 0, "sessions": 0, "versions": {}, "sources": [],
                                        "packs": {}, "tokens_in_sessions": 0, "feedback_up": 0, "feedback_down": 0,
                                        "notes": [], "corrections_after": 0, "days_used": [], "last_used": ""})
    for e in events:
        kind, sid = e.get("kind"), e.get("session", "")
        if kind == "use":
            s = sk(e["skill"])
            s["uses"] += 1
            day = str(e.get("at", ""))[:10]
            if day and day not in s["days_used"]:
                s["days_used"].append(day)
            s["last_used"] = max(s["last_used"], day)
            if e.get("version"):
                s["versions"][e["version"]] = s["versions"].get(e["version"], 0) + 1
            if e.get("source") and e["source"] not in s["sources"]:
                s["sources"].append(e["source"])
            if e.get("pack"):
                s["packs"][e["pack"]] = s["packs"].get(e["pack"], 0) + 1
                packs.setdefault(e["pack"], set()).add(sid)
            sessions.setdefault(sid, {"skills": set(), "tokens": 0})["skills"].add(e["skill"])
        elif kind == "session":
            ss = sessions.setdefault(sid, {"skills": set(), "tokens": 0})
            ss["tokens"] = e.get("tokens", {}).get("total", 0)
            ss["skills"] |= {k for k in e.get("tokens", {}).get("skills", []) if k.split(":")[-1] not in OWN_SKILLS}
            ss["ended"] = True
        elif kind == "feedback":
            s = sk(e.get("skill") or "(no skill)")
            s["feedback_up" if e.get("rating") == "up" else "feedback_down"] += 1
            if e.get("note"):
                s["notes"].append({"rating": e.get("rating"), "note": e["note"][:500]})
        elif kind == "correction" and e.get("skill"):
            sk(e["skill"])["corrections_after"] += 1
    no_skill = [v for v in sessions.values() if v.get("ended") and not v["skills"]]
    for s in skills.values():
        s["days_used"] = len(s["days_used"])
    for v in sessions.values():
        for name in v["skills"]:
            s = sk(name)
            s["sessions"] += 1
            s["tokens_in_sessions"] += v["tokens"]
    period = {"week": week, "days": days or [date]} if week else {}
    return {"schema": SCHEMA, "client": "claude-code", "user": user, "date": date, **period,
            "sessions": sum(1 for v in sessions.values() if v.get("ended")),
            "sessions_with_skills": sum(1 for v in sessions.values() if v["skills"]),
            "no_skill_sessions": len(no_skill), "tokens_no_skill_sessions": sum(v["tokens"] for v in no_skill),
            "packs": {p: {"sessions": len(v)} for p, v in sorted(packs.items())},
            "skills": skills}


def read_events(date: str) -> list[dict]:
    f = home() / "events" / f"{date}.jsonl"
    out = []
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out
