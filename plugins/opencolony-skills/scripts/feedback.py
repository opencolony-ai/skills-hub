#!/usr/bin/env python3
"""Feedback on a skill, command or subagent: up or down, optionally which one, optionally a note.

In Claude Code (the hook calls main):
    /skill-feedback up
    /skill-feedback down too long, missed the second table
    /skill-feedback down weekly-report wrong date format
The item defaults to the last one Claude used in this session.

From another program, such as the project platform's 👍/👎 buttons (prints JSON):
    feedback.py recent --project DIR [--days 7]       what was used in that project lately, to ask about
    feedback.py give --skill NAME --rating up|down [--note TEXT] [--project DIR]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

UP = {"up", "+", "+1", "good", "yes", "👍", "好", "有用", "有幫助"}
DOWN = {"down", "-", "-1", "bad", "no", "👎", "不好", "沒用", "沒幫助"}


def parse(args: list[str], used: list[str]) -> tuple[str | None, str | None, str]:
    words = " ".join(args).split()
    if not words or words[0].lower() not in UP | DOWN:
        return None, None, ""
    rating = "up" if words[0].lower() in UP else "down"
    rest = words[1:]
    skill = None
    match = [u for u in used if rest and (u == rest[0] or u.lstrip("/@").split(":")[-1] == rest[0].lstrip("/@"))]
    if match:
        skill, rest = match[0], rest[1:]
    elif used:
        skill = used[-1]
    return rating, skill, " ".join(rest)


def record(skill: str | None, rating: str, note: str, session: str = "", project: str = "", source: str = "") -> None:
    common.append({"kind": "feedback", "session": session, "skill": skill, "rating": rating, "note": note[:500],
                   **({"project": project} if project else {}), **({"from": source} if source else {})})
    if session:
        st = common.state(session)
        st["rated"] = sorted(set(st.get("rated", [])) | {skill})
        st.setdefault("ratings", {})[skill or ""] = rating      # /save-as-case knows whether the answer was liked
        common.save_state(session, st)


def to_owner(skill: str | None, rating: str, note: str, cwd: str) -> str:
    """Start sending a sentence of feedback to the skill's owner (in the background); says where it is going, or ""."""
    if not note.strip() or not skill:
        return ""
    import subprocess
    import suggest
    if suggest.is_public(cwd):
        return ""
    host, owner, repo = suggest.destination(skill, cwd)
    if not owner:
        return ""
    subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "suggest.py"), "quick", "--skill", skill,
                      "--rating", rating, "--note", note, "--cwd", cwd, "--notify"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ.copy(), **common.detach_kwargs())
    return f"{owner}/{repo}"


def main(args: list[str], cwd: str = "") -> str:
    f = common.home() / "last_session"
    sid = f.read_text(encoding="utf-8").strip() if f.is_file() else ""
    used = common.state(sid).get("skills", []) if sid else []
    rating, skill, note = parse(args, used)
    if not rating:
        return "Usage: /skill-feedback up|down [skill] [a note]. Nothing recorded."
    record(skill, rating, note, sid, project=common.project_id(cwd) if cwd else "")
    what = f"for {skill}" if skill else "(no skill was used in this session)"
    to = to_owner(skill, rating, note, cwd)
    return (f"Recorded {'👍' if rating == 'up' else '👎'} {what}" + (f": “{note}”" if note else "") + "."
            + (f" Sending it to the owner ({to})." if to else ""))


def recent(project: str, days: int) -> list[dict]:
    """Items used in this project in the last days, newest first, with whether they were rated since."""
    pid = common.project_id(project)
    seen: dict[str, dict] = {}
    for i in range(days, -1, -1):
        date = (dt.date.today() - dt.timedelta(days=i)).isoformat()
        for e in common.read_events(date):
            k = e.get("skill")
            if e.get("kind") == "use" and e.get("project") == pid:
                seen[k] = {"name": k, "type": e.get("type", "skill"), "last_used": e.get("at"), "rated": False,
                           "version": e.get("version", "")}
            elif e.get("kind") == "feedback" and k in seen:
                seen[k]["rated"] = True
    return sorted(seen.values(), key=lambda x: x["last_used"] or "", reverse=True)


def cli(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("recent"); r.add_argument("--project", default="."); r.add_argument("--days", type=int, default=7)
    g = sub.add_parser("give"); g.add_argument("--skill", required=True); g.add_argument("--rating", required=True, choices=["up", "down"])
    g.add_argument("--note", default=""); g.add_argument("--project", default="")
    a = ap.parse_args(argv)
    if a.cmd == "recent":
        common.write_stdout_json({"items": recent(a.project, a.days)})
    else:
        record(a.skill, a.rating, a.note, project=common.project_id(a.project) if a.project else "", source="cli")
        common.write_stdout_json({"recorded": True, "skill": a.skill, "rating": a.rating})


if __name__ == "__main__":
    if sys.argv[1:2] and sys.argv[1] in ("recent", "give", "-h", "--help"):
        cli(sys.argv[1:])
    else:
        print(main(sys.argv[1:]))
