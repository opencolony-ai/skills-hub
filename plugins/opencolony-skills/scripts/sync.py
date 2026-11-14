#!/usr/bin/env python3
"""Share finished days with the company's skills hub repository: for every day before today that has events
and has not been shared yet, write usage/<user>/<date>.json into a clone of the hub, recompute the dashboard
(data/, DASHBOARD.md) from everyone's usage, commit and push with the person's own git credentials. The hub is
the repository the plugin was installed from (its marketplace), or "hub" in ~/.opencolony/skills/config.json.
Started in the background at the start of a session; quiet when anything fails (no hub configured, offline, no
permission) and tries again next time.

    python3 sync.py            share what is due
    python3 sync.py --dry-run  print what would be shared
    python3 sync.py --include-today   share today too (to try it out; today is shared again tomorrow, complete)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def git(args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def due(now=None) -> list[str]:
    done = set(json.loads((common.home() / "synced.json").read_text())) if (common.home() / "synced.json").is_file() else set()
    t = common.today(now)
    return sorted(f.stem for f in (common.home() / "events").glob("*.jsonl") if f.stem < t and f.stem not in done)


def week_summaries(days: list[str], user: str, now=None) -> dict[str, dict]:
    """One summary per week touched by the days due, over every finished day of that week: the week's file is
    rewritten in place as the week goes on (thousands of people, one small file each per week)."""
    t = common.today(now)
    out = {}
    for week in sorted({common.week_of(d) for d in days}):
        finished = [d for d in common.week_days(week) if d < t and (common.home() / "events" / f"{d}.jsonl").is_file()]
        events = [e for d in finished for e in common.read_events(d)]
        out[week] = common.summarise(events, user, finished[-1] if finished else week, week=week, days=finished)
    return out


def user_handle(cfg) -> str:
    return common.user_handle()


def detect_hub() -> str:
    """The hub this plugin was installed from: the marketplace that lists opencolony-skills and has a hub.json,
    preferring the one this copy of the plugin came from. Its git remote is where usage goes."""
    claude = common.claude_dir()
    try:
        known = json.loads((claude / "plugins" / "known_marketplaces.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    root = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parents[1]).parts
    mine = root[root.index("cache") + 1] if "cache" in root and root.index("cache") + 1 < len(root) else ""
    for name in sorted(known, key=lambda n: n != mine):
        m = known[name] or {}
        loc = Path(m.get("installLocation") or "")
        if not (loc / "hub.json").is_file():
            continue
        try:
            listed = {p.get("name") for p in json.loads((loc / ".claude-plugin" / "marketplace.json").read_text()).get("plugins", [])}
        except (OSError, ValueError):
            continue
        if "opencolony-skills" not in listed:
            continue
        url = git(["remote", "get-url", "origin"], cwd=loc).stdout.strip() if (loc / ".git").exists() else ""
        src = m.get("source") or {}
        if not url and src.get("source") == "github" and src.get("repo"):
            url = f"https://github.com/{src['repo']}.git"
        if not url and src.get("source") == "git" and src.get("url"):
            url = src["url"]
        if url:
            return url
    return ""


def usage_mode(clone: Path | None = None) -> str:
    """hub.json's "usage": "git" (people push their usage to the hub; the default) or "none" (a public hub, which
    nobody can push to: nothing is sent and nothing is tried)."""
    try:
        return str(json.loads(((clone or common.home() / "hub") / "hub.json").read_text(encoding="utf-8")).get("usage") or "git")
    except (OSError, ValueError):
        return "git"


def hub_url(cwd: str = "") -> str:
    return common.config().get("hub") or common.project_lock(cwd).get("hub") or detect_hub()


def publish(place, message: str, cwd: str = "", rebuild_now: bool = False) -> bool:
    """Put files into the hub and push: place(clone) writes them and returns the paths to add. Starts again
    from the hub's latest commit when someone else pushed in between, and rebuilds the dashboard each time."""
    hub = hub_url(cwd)
    if not hub or usage_mode() == "none":
        return False
    clone = common.home() / "hub"
    if not (clone / ".git").is_dir():
        if git(["clone", "-q", "--filter=blob:none", hub, str(clone)]).returncode != 0:
            return False
    elif git(["remote", "get-url", "origin"], cwd=clone).stdout.strip() != hub:
        git(["remote", "set-url", "origin", hub], cwd=clone)
    import random
    import time
    for attempt in range(6):
        if attempt:
            time.sleep(random.uniform(1, 4) * attempt)      # many people share at the start of the day
        if git(["fetch", "-q", "origin"], cwd=clone).returncode != 0:
            return False
        branch = git(["rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=clone).stdout.strip() or "origin/main"
        git(["checkout", "-q", "-B", branch.split("/", 1)[1], branch], cwd=clone)
        git(["reset", "-q", "--hard", branch], cwd=clone)
        paths = place(clone)
        if rebuild_now or stale(clone):     # not on every push: thousands of people share each day
            rebuild(clone)
        git(["add", "-A", *paths, "data", "DASHBOARD.md"], cwd=clone)
        if git(["commit", "-q", "-m", message], cwd=clone).returncode != 0:
            return False
        if git(["push", "-q", "origin", "HEAD"], cwd=clone).returncode == 0:
            return True
    return False


def evals_due() -> list[Path]:
    done = set(json.loads((common.home() / "synced_evals.json").read_text())) if (common.home() / "synced_evals.json").is_file() else set()
    return sorted(f for f in (common.home() / "evals").glob("*/result.json") if f.parent.name not in done)


def shared_eval(f: Path) -> dict:
    """What is shared of an evaluation: counts only, never the requests or the answers."""
    r = json.loads(f.read_text(encoding="utf-8"))
    return {k: r.get(k) for k in ("schema", "skill", "user", "date", "versions", "cases", "times", "arms", "verdict")}


def run(dry=False, now=None, cwd: str = "", rebuild_now: bool = False) -> list[str]:
    if usage_mode() == "none":
        return []
    days, evals = due(now), evals_due()
    if not days and not evals:
        return []
    user = user_handle(common.config())
    summaries = week_summaries(days, user, now)
    if dry:
        print(json.dumps({"usage": summaries, "evaluations": [shared_eval(f) for f in evals]}, ensure_ascii=False, indent=1))
        return days

    def place(clone: Path) -> list[str]:
        for week, s in summaries.items():
            f = clone / "usage" / user / f"{week}.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(s, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        for e in evals:
            f = clone / "evals" / user / f"{e.parent.name}.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(shared_eval(e), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return [p for p in ("usage", "evals") if (clone / p).exists()]
    what = f"usage: {user} {days[0]}..{days[-1]}" if days else f"evaluations: {user}"
    if not publish(place, what, cwd, rebuild_now=rebuild_now):
        return []
    for name, items in (("synced.json", days), ("synced_evals.json", [e.parent.name for e in evals])):
        f = common.home() / name
        done = set(json.loads(f.read_text())) if f.is_file() else set()
        f.write_text(json.dumps(sorted(done | set(items))))
    return days or ["evaluations"]


def stale(clone: Path) -> bool:
    """The dashboard is older than hub.json "rebuild_hours" (default 1)."""
    try:
        hours = float(json.loads((clone / "hub.json").read_text(encoding="utf-8")).get("rebuild_hours", 1))
    except (OSError, ValueError, TypeError):
        hours = 1
    try:
        made = json.loads((clone / "data" / "stats.json").read_text(encoding="utf-8")).get("made", "")
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(made)).total_seconds() > hours * 3600
    except (OSError, ValueError, TypeError):
        return True


def rebuild(clone: Path) -> None:
    """Recompute data/ and DASHBOARD.md with everyone's usage, so the dashboard needs no GitHub Actions.
    Uses this plugin's own copy of the hub tools, never code from the clone."""
    import contextlib
    import io
    import hub
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            hub.main(["--root", str(clone), "all"])
    except Exception:       # the usage is still shared; the next person's sync tries again
        pass


def locked() -> bool:
    """One sync at a time (two sessions may start together); a lock older than ten minutes is stale."""
    import time
    f = common.home() / "sync.lock"
    common.home().mkdir(parents=True, exist_ok=True)
    try:
        if f.exists() and time.time() - f.stat().st_mtime > 600:
            f.unlink()
        os.close(os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return False
    except OSError:
        return True


if __name__ == "__main__":
    a = sys.argv[1:]
    cwd = a[a.index("--cwd") + 1] if "--cwd" in a and a.index("--cwd") + 1 < len(a) else ""
    later = dt.datetime.now() + dt.timedelta(days=1) if "--include-today" in a else None    # for trying it out
    if "--dry-run" in a:
        run(dry=True, cwd=cwd, now=later)
    elif not locked():
        try:
            done = run(cwd=cwd, now=later, rebuild_now=later is not None)       # trying it out: show the dashboard now
            if "--include-today" in a:
                print(json.dumps({"shared": done}, ensure_ascii=False))
        except Exception as e:
            if "--include-today" in a:
                print(f"not shared: {e}")
        finally:
            (common.home() / "sync.lock").unlink(missing_ok=True)
