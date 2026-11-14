#!/usr/bin/env python3
"""The company's skills hub: which skills exist, which are used, and what people say about them.

Standard library only. The opencolony-skills plugin runs `all` on the hub after it shares a person's usage, so the
dashboard stays current without GitHub Actions; the hub's optional workflow runs the same thing nightly.

    python hub/hub.py inventory [--scan DIR ...]   find every skill (SKILL.md) in the given checkouts and in skills/,
                                                   with its owner, version and look-alikes -> data/inventory.json
    python hub/hub.py aggregate [--days 30]        add up usage/<user>/<date>.json -> data/stats.json
    python hub/hub.py dashboard [--out site]       the page people read -> site/index.html and DASHBOARD.md
    python hub/hub.py all                          the three in turn

Usage files are written by the opencolony-skills plugin on each person's machine. Every number on the page comes
from these files; anyone with the repository can recompute it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pull  # noqa: E402  (the packs, read the same way pull.py reads them)

ROOT = Path.cwd()   # the hub repository; main() sets it from --root
DEAD_AFTER_DAYS = 30


def esc(x) -> str:
    return html.escape(str(x), quote=True)


# ------------------------------------------------------------------ inventory

def front_matter(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    out = {}
    if m:
        key = None
        for line in m.group(1).splitlines():
            kv = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
            if kv:
                key = kv.group(1)
                out[key] = kv.group(2).strip().strip('"').strip("'")
            elif key and line.startswith((" ", "\t")):
                out[key] = (out[key] + " " + line.strip()).strip()
    for k in list(out):
        if out[k] in (">", "|", ">-", "|-"):
            out[k] = ""
    return out


def git_info(path: Path) -> dict:
    """Last commit and its author for one file, when it sits in a git checkout."""
    def run(*a):
        try:
            return subprocess.run(["git", *a], cwd=path.parent, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""
    log = run("log", "-1", "--format=%H%x09%an%x09%cs", "--", path.name)
    remote = run("config", "--get", "remote.origin.url")
    sha, author, date = (log.split("\t") + ["", "", ""])[:3] if log else ("", "", "")
    repo = re.sub(r"^.*[:/]([^/:]+/[^/]+?)(\.git)?$", r"\1", remote) if remote else ""
    return {"commit": sha[:12], "last_author": author, "last_changed": date, "repo": repo}


def owner_of(path: Path) -> str:
    """The skill's owner: `owner:` or `author:` in its front matter is read first by the caller; else a
    CODEOWNERS entry that covers it; else the last person who changed it."""
    for d in [path.parent, *path.parents]:
        for co in (d / "CODEOWNERS", d / ".github" / "CODEOWNERS"):
            if co.is_file():
                rel = str(path.relative_to(d))
                best = ""
                for line in co.read_text(encoding="utf-8", errors="ignore").splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and not line.startswith("#"):
                        pat = parts[0].lstrip("/").rstrip("/")
                        if rel.startswith(pat.replace("*", "")) or pat == "*":
                            best = " ".join(parts[1:])
                if best:
                    return best
        if (d / ".git").exists():
            break
    return ""


def words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9一-鿿]{3,}", (s or "").lower())} - {"use", "when", "the", "and", "for", "with", "this", "that", "skill"}


SKIP = {".git", "node_modules", "usage", "data", "site", ".scan", "cases", "evals"}


KINDS = {"skill": "", "command": "/", "agent": "@"}
OWN_COMMANDS = {"skill-feedback", "save-as-case", "skill-eval", "send-suggestion"}      # the app's own, in every project


def items_in(root: Path):
    """(kind, file) for every skill, command and subagent under root: skills/<name>/SKILL.md anywhere,
    commands/<name>.md and agents/<name>.md inside .claude, .github, .agents or a plugin."""
    for f in sorted(root.rglob("SKILL.md")):
        yield "skill", f
    for kind, folder in (("command", "commands"), ("agent", "agents")):
        for f in sorted(root.rglob(f"{folder}/*.md")):
            parts = f.relative_to(root).parts
            if len(parts) >= 3 and (parts[-3] in (".claude", ".github", ".agents") or (len(parts) >= 4 and parts[-4] == "plugins")) \
                    and f.name.lower() != "readme.md" and not (kind == "command" and f.stem in OWN_COMMANDS):
                yield kind, f


def inventory(scan: list[Path]) -> dict:
    """Every skill, command and subagent in the hub repository itself and in the checkouts given (other
    repositories that keep them in .claude, .github, .agents or a plugin), and the packs."""
    skills = []
    seen = set()
    for root in [ROOT, *scan]:
        if not root.exists():
            continue
        for kind, f in items_in(root):
            rel = f.relative_to(root).as_posix()
            if SKIP & set(rel.split("/")[:-1]) or pull.TRACKER in rel.split("/"):
                continue
            raw = f.read_bytes()
            fm = front_matter(raw.decode("utf-8", errors="replace"))
            name = KINDS[kind] + ((fm.get("name") or f.parent.name) if kind == "skill" else f.stem)
            info = git_info(f)
            key = (info["repo"] or str(root), rel)
            if key in seen:
                continue
            seen.add(key)
            skills.append({"name": name, "type": kind, "description": fm.get("description", "")[:600], "path": rel,
                           "repo": info["repo"] or root.name, "version": hashlib.sha256(raw).hexdigest()[:12],
                           "owner": fm.get("owner") or fm.get("author") or owner_of(f) or info["last_author"],
                           "last_changed": info["last_changed"], "commit": info["commit"],
                           "by_name_only": fm.get("disable-model-invocation", "").lower() == "true",
                           "feedback": fm.get("feedback", "")})
    # look-alikes: the same name in two places, or descriptions that share most of their words
    for s in skills:
        s["same_name"] = [f"{o['repo']}/{o['path']}" for o in skills if o is not s and o["name"] == s["name"]]
        ws = words(s["description"])
        s["similar"] = []
        for o in skills:
            if o is s or o["name"] == s["name"]:
                continue
            wo = words(o["description"])
            if ws and wo and len(ws & wo) / len(ws | wo) >= 0.5:
                s["similar"].append(o["name"])
    return {"schema": 1, "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "skills": skills,
            "packs": pull.packs(ROOT) if (ROOT / "packs").is_dir() else []}


# ------------------------------------------------------------------ aggregate

def short_name(raw: str) -> str:
    """team-skills:weekly-report (from the plugin) and weekly-report (pulled into a project) are one skill;
    the same for /team-skills:standup and /standup."""
    mark = raw[:1] if raw[:1] in "/@" else ""
    return mark + raw[len(mark):].split(":")[-1]


def aggregate(days: int, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    since = (today - dt.timedelta(days=days)).isoformat()
    per: dict[str, dict] = {}
    packs: dict[str, dict] = {}
    totals = {"people": set(), "sessions": 0, "sessions_with_skills": 0, "no_skill_sessions": 0, "tokens_no_skill_sessions": 0}
    for f in sorted((ROOT / "usage").glob("*/*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("schema") != 1 or str(d.get("date", "")) < since:
            continue
        user = str(d.get("user", f.parent.name))
        totals["people"].add(user)
        for k in ("sessions", "sessions_with_skills", "no_skill_sessions", "tokens_no_skill_sessions"):
            totals[k] += int(d.get(k) or 0)
        for pk, v in (d.get("packs") or {}).items():
            packs.setdefault(pk, {"name": pk, "sessions": 0, "people": set(), "uses": {}})["sessions"] += int(v.get("sessions") or 0)
        for raw, s in (d.get("skills") or {}).items():
            name = short_name(raw)
            p = per.setdefault(name, {"name": name, "type": s.get("type") or ("command" if name.startswith("/") else
                                      "agent" if name.startswith("@") else "skill"), "packs": {},
                                      "uses": 0, "sessions": 0, "people": set(), "tokens_in_sessions": 0,
                                      "up": 0, "down": 0, "corrections_after": 0, "notes": [], "versions": {},
                                      "sources": set(), "last_used": "", "days": set()})
            p.setdefault("names", set()).add(raw)
            p["uses"] += int(s.get("uses") or 0)
            p["sessions"] += int(s.get("sessions") or 0)
            p["tokens_in_sessions"] += int(s.get("tokens_in_sessions") or 0)
            p["up"] += int(s.get("feedback_up") or 0)
            p["down"] += int(s.get("feedback_down") or 0)
            p["corrections_after"] += int(s.get("corrections_after") or 0)
            if s.get("uses"):
                p["people"].add(user)
                if d.get("week"):                  # a week's file: one summary over its days
                    p["days"] |= {f"{user}:{d['week']}:{i}" for i in range(int(s.get("days_used") or 1))}
                else:
                    p["days"].add(f"{user}:{d['date']}")
                p["last_used"] = max(p["last_used"], s.get("last_used") or d["date"])
            for v, n in (s.get("versions") or {}).items():
                p["versions"][v] = p["versions"].get(v, 0) + int(n)
            p["sources"] |= set(s.get("sources") or [])
            for pk, n in (s.get("packs") or {}).items():
                p["packs"][pk] = p["packs"].get(pk, 0) + int(n)
                pp = packs.setdefault(pk, {"name": pk, "sessions": 0, "people": set(), "uses": {}})
                pp["people"].add(user)
                pp["uses"][name] = pp["uses"].get(name, 0) + int(n)
            for n in s.get("notes") or []:
                p["notes"].append({"date": d["date"], "rating": n.get("rating"), "note": str(n.get("note", ""))[:500]})
    out = []
    for p in per.values():
        p["people"], p["sources"], p["days"], p["names"] = len(p["people"]), sorted(p["sources"]), len(p["days"]), sorted(p["names"])
        p["notes"] = sorted(p["notes"], key=lambda n: n["date"], reverse=True)[:20]
        p["tokens_per_session"] = round(p["tokens_in_sessions"] / p["sessions"]) if p["sessions"] else None
        out.append(p)
    nts = totals["no_skill_sessions"]
    for pp in packs.values():
        pp["people"] = len(pp["people"])
    evals, cases = {}, {}
    for f in sorted((ROOT / "evals").glob("*/*.json")):      # the latest evaluation of each skill
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        k = short_name(str(e.get("skill", "")))
        if e.get("schema") == 1 and (k not in evals or (e.get("date", ""), f.name) >= (evals[k]["date"], evals[k]["_file"])):
            evals[k] = {**{x: e.get(x) for x in ("date", "user", "versions", "cases", "times", "arms", "verdict")}, "_file": f.name}
    for f in (ROOT / "cases").glob("*/*/case.json"):
        cases[f.parent.parent.name] = cases.get(f.parent.parent.name, 0) + 1
    return {"schema": 1, "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "days": days, "since": since,
            "packs": sorted(packs.values(), key=lambda x: (-x["sessions"], x["name"])),
            "evals": {k: {x: v for x, v in e.items() if x != "_file"} for k, e in sorted(evals.items())},
            "cases": dict(sorted(cases.items())),
            "people": len(totals["people"]), "sessions": totals["sessions"], "sessions_with_skills": totals["sessions_with_skills"],
            "tokens_per_session_without_skills": round(totals["tokens_no_skill_sessions"] / nts) if nts else None,
            "skills": sorted(out, key=lambda p: (-p["uses"], p["name"]))}


# ------------------------------------------------------------------ dashboard

def issues_url(item_name: str, spec: str) -> str:
    """Where a skill's suggestions are: the issues of its feedback repository (its `feedback:`, else hub.json
    "feedback", else this hub), searched for its name."""
    spec = spec or settings().get("feedback") or ""
    if not spec:
        try:
            spec = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True, text=True,
                                  timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""
    m = re.match(r"^(?:https?://|ssh://)?(?:[^@/]+@)?([^/:]+)[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", spec)
    if m and "." in m.group(1):
        host, owner, repo = m.groups()
    elif re.match(r"^[\w.-]+/[\w.-]+$", spec):
        host, (owner, repo) = "github.com", spec.split("/")
    else:
        return ""
    q = urllib.parse.quote(f"is:issue [{item_name.lstrip('/@').split(':')[-1]}]")
    return f"https://{host}/{owner}/{repo}/issues?q={q}"


def merge(inv: dict, stats: dict) -> list[dict]:
    """One row per skill: in the inventory, used, or both."""
    by = {}
    for s in inv.get("skills", []):
        by.setdefault(s["name"], {"name": s["name"], "known": [], "stats": None})["known"].append(s)
    for p in stats.get("skills", []):
        row = by.get(p["name"]) or by.get(short_name(p["name"])) or by.setdefault(p["name"], {"name": p["name"], "known": [], "stats": None})
        row["stats"] = p
    for r in by.values():
        st, known = r["stats"], r["known"]
        latest = {k["version"] for k in known}
        r["status"] = ("not in the inventory" if not known else
                       "unused" if not st or not st["uses"] else
                       "used")
        r["old_versions"] = sorted(v for v in (st or {}).get("versions", {}) if v and latest and v not in latest)
    order = {"used": 0, "not in the inventory": 1, "unused": 2}
    return sorted(by.values(), key=lambda r: (order[r["status"]], -((r["stats"] or {}).get("uses") or 0), r["name"]))


CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--mute:#5d5b56;--line:#dcd9d1;--card:#fff;--good:#17703a;--bad:#b3261e;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecece8;--mute:#a7a59e;--line:#3a3a36;--card:#1f1f1d;--good:#5cc489;--bad:#ff8a80;--warn:#f0b54a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:28px;margin:0 0 4px}.mute{color:var(--mute)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}.tile b{display:block;font-size:26px}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px;background:var(--card)}
th,td{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--mute);font-weight:500}
.n{text-align:right;font-variant-numeric:tabular-nums}.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}
details{margin:2px 0}summary{cursor:pointer}ul{margin:4px 0;padding-left:18px}h2{font-size:20px;margin:28px 0 8px}
"""

TEXT = {
    "en": {"title": "Skills hub", "intro": "Which skills people use, and what they say about them, over the last {days} days (since {since}). "
           "Counted by the opencolony-skills plugin on each person's machine: only counts, tokens and the notes people typed as feedback "
           "are shared, never prompts, answers or file names. Updated {made}.",
           "t_inv": "skills in the inventory", "t_used": "skills used", "t_dead": "not used in {days} days", "t_people": "people sharing usage",
           "t_sessions": "sessions", "in_use": "In use", "in_use_help": "Corrected after: times the person said \"no, that's wrong\" (or the like) "
           "right after the skill was used. Tokens / session: all tokens of the sessions that used the skill. People's own "
           "cases: the latest /skill-eval run of the cases people saved with /save-as-case.",
           "base": " Sessions that used no skill average {base} tokens.",
           "skill": "Skill", "status": "Status", "uses": "Uses", "people": "People", "days_used": "Days used", "corr": "Corrected after",
           "tps": "Tokens / session", "owner": "Owner", "notes": "Notes", "where": "Where", "changed": "Last changed",
           "used": "used", "not in the inventory": "used, not in the inventory", "none_yet": "No usage shared yet.", "none": "None.",
           "dead": "Not used in {days} days", "dead_help": "In the inventory, but nobody sharing usage used them.",
           "old": "{n} version(s) in use that are not in the hub (older, or changed in a project)", "same": "same name elsewhere: {x}", "like": "looks like: {x}",
           "packs": "By project type", "packs_help": "What each pack (the skills, commands and subagents pulled into one type of "
           "project) holds, and how much of it is used.", "pack": "Project type", "items": "Holds", "sessions": "Sessions",
           "top": "Most used", "missing": "not in the hub: {x}", "no_packs": "No packs yet (packs/*.json).",
           "own_cases": "People's own cases", "ev": "passes {a} with it, {b} without, tokens {t:+d}% ({d})",
           "shared_cases": "{n} shared case(s)", "suggestions": "suggestions"},
    "zh-TW": {"title": "技能中心", "intro": "最近 {days} 天（{since} 起）大家用了哪些技能、怎麼評價。由每個人電腦上的 opencolony-skills 外掛記錄，"
              "只分享次數、token 數和大家主動留下的回饋，不分享提示、回答或檔名。更新於 {made}。",
              "t_inv": "盤點到的技能", "t_used": "有人用的技能", "t_dead": "{days} 天沒人用", "t_people": "分享用量的人", "t_sessions": "工作階段",
              "in_use": "使用中", "in_use_help": "用完被糾正：技能用過之後，對方緊接著說「不對」之類的次數。每階段 token：用到該技能的工作階段的全部 token。"
              "使用者自己的考題：大家用 /save-as-case 存下的考題，最近一次 /skill-eval 的結果。",
              "base": "沒用任何技能的工作階段平均 {base} token。",
              "skill": "技能", "status": "狀態", "uses": "次數", "people": "人數", "days_used": "使用天數", "corr": "用完被糾正",
              "tps": "每階段 token", "owner": "負責人", "notes": "回饋", "where": "位置", "changed": "最後修改",
              "used": "使用中", "not in the inventory": "有人用，但不在盤點裡", "none_yet": "還沒有人分享用量。", "none": "沒有。",
              "dead": "{days} 天沒人用", "dead_help": "盤點得到，但分享用量的人都沒用過。",
              "old": "有 {n} 個版本不在 hub 裡（舊版，或在專案裡被改過）", "same": "別處有同名技能：{x}", "like": "和這些很像：{x}",
              "packs": "依專案類型", "packs_help": "每個 pack（某類專案會拉進去的技能、指令、subagent）包含什麼、實際用了多少。",
              "pack": "專案類型", "items": "內容", "sessions": "工作階段", "top": "最常用", "missing": "hub 裡找不到：{x}",
              "no_packs": "還沒有 pack（packs/*.json）。",
              "own_cases": "使用者自己的考題", "ev": "用它通過 {a}、不用 {b}，token {t:+d}%（{d}）", "shared_cases": "分享了 {n} 題", "suggestions": "建議"},
}


def md(x) -> str:
    """Text for a Markdown table cell: no HTML, no Markdown, no line breaks."""
    s = html.escape(" ".join(str(x).split()), quote=False)
    return re.sub(r"([\\`*_\[\]|!~])", lambda m: f"&#{ord(m.group(1))};", s)


def dashboard(inv: dict, stats: dict, out: Path, lang: str = "en") -> Path:
    """site/index.html (for GitHub Pages or any web server) and DASHBOARD.md (GitHub shows it to whoever can read
    the repository; the safe default for a private hub)."""
    T = TEXT.get(lang, TEXT["en"])
    rows = merge(inv, stats)
    used = [r for r in rows if r["status"] != "unused"]
    unused = [r for r in rows if r["status"] == "unused"]
    days = stats.get("days", DEAD_AFTER_DAYS)
    base = stats.get("tokens_per_session_without_skills")
    intro = T["intro"].format(days=days, since=stats.get("since", "–"), made=stats.get("made", "–"))
    help_ = T["in_use_help"] + (T["base"].format(base=f"{base:,}") if base else "")
    tiles = [(T["t_inv"], len(inv.get("skills", []))), (T["t_used"], sum(1 for r in rows if r["stats"] and r["stats"]["uses"])),
             (T["t_dead"].format(days=days), len(unused)), (T["t_people"], stats.get("people", 0)), (T["t_sessions"], stats.get("sessions", 0))]
    cols = ["skill", "status", "uses", "people", "days_used", "👍", "👎", "corr", "tps", "own_cases", "owner", "notes"]

    def eval_text(name: str) -> str:
        e = (stats.get("evals") or {}).get(name)
        n = (stats.get("cases") or {}).get(name.lstrip("/@"), 0)
        parts = []
        if e and (e.get("verdict") or {}).get("current_vs_none"):
            v = e["verdict"]["current_vs_none"]
            parts.append(T["ev"].format(a=f"{v['passed'][0]}/{v['passed'][1]}", b=f"{v['passed_before'][0]}/{v['passed_before'][1]}",
                                        t=v["tokens_change_pct"], d=e.get("date", "")))
        if n:
            parts.append(T["shared_cases"].format(n=n))
        return " · ".join(parts) or "–"
    label = lambda c: T.get(c, c)

    def facts(r):
        st, k = r["stats"] or {}, (r["known"] or [{}])[0]
        warn = []
        if r["old_versions"]:
            warn.append(T["old"].format(n=len(r["old_versions"])))
        if k.get("same_name"):
            warn.append(T["same"].format(x=", ".join(k["same_name"])))
        if k.get("similar"):
            warn.append(T["like"].format(x=", ".join(k["similar"])))
        return st, k, warn, st.get("notes") or []

    def issues_html(name, k):
        u = issues_url(name, k.get("feedback", "")) if k else ""
        return f"<div><a href=\"{esc(u)}\">{esc(T['suggestions'])}</a></div>" if u else ""

    def issues_md(name, k):
        u = issues_url(name, k.get("feedback", "")) if k else ""
        return f"<br>[{md(T['suggestions'])}]({u.replace(' ', '%20').replace(')', '%29')})" if u else ""

    body, mdrows = [], []
    for r in used:
        st, k, warn, notes = facts(r)
        tps = f"{st['tokens_per_session']:,}" if st.get("tokens_per_session") else "–"
        note_html = ("<details><summary>%d</summary><ul>%s</ul></details>" % (len(notes), "".join(
            f"<li class={'good' if n['rating'] == 'up' else 'bad'}>{esc(n['date'])} {'👍' if n['rating'] == 'up' else '👎'} {esc(n['note'])}</li>"
            for n in notes))) if notes else ""
        body.append(f"<tr><td><b>{esc(r['name'])}</b><div class=mute>{esc(k.get('repo', ''))}</div>"
                    f"{''.join(f'<div class=warn>{esc(w)}</div>' for w in warn)}</td><td>{esc(T[r['status']])}</td>"
                    f"<td class=n>{st.get('uses', 0)}</td><td class=n>{st.get('people', 0)}</td><td class=n>{st.get('days', 0)}</td>"
                    f"<td class='n good'>{st.get('up', 0)}</td><td class='n bad'>{st.get('down', 0)}</td>"
                    f"<td class=n>{st.get('corrections_after', 0)}</td><td class=n>{tps}</td><td>{esc(eval_text(r['name']))}</td>"
                    f"<td>{esc(k.get('owner', ''))}{issues_html(r['name'], k)}</td>"
                    f"<td>{note_html}</td></tr>")
        mdnotes = "<br>".join(f"{md(n['date'])} {'👍' if n['rating'] == 'up' else '👎'} {md(n['note'])}" for n in notes[:5])
        mdrows.append("| " + " | ".join([f"**{md(r['name'])}**" + "".join(f"<br>⚠ {md(w)}" for w in warn), md(T[r["status"]]),
                                          str(st.get("uses", 0)), str(st.get("people", 0)), str(st.get("days", 0)), str(st.get("up", 0)),
                                          str(st.get("down", 0)), str(st.get("corrections_after", 0)), tps, md(eval_text(r["name"])),
                                          md(k.get("owner", "")) + issues_md(r["name"], k), mdnotes]) + " |")
    seen_packs = {p["name"]: p for p in inv.get("packs", [])}
    used_packs = {p["name"]: p for p in stats.get("packs", [])}
    pack_rows = []
    for name in list(seen_packs) + [n for n in used_packs if n not in seen_packs]:
        a, b = seen_packs.get(name, {}), used_packs.get(name, {})
        holds = ", ".join(i["name"] if i["type"] == "skill" else ("/" if i["type"] == "command" else "@") + i["name"] for i in a.get("items", []))
        if a.get("missing"):
            holds += ("; " if holds else "") + T["missing"].format(x=", ".join(a["missing"]))
        top = ", ".join(f"{k} ({v})" for k, v in sorted((b.get("uses") or {}).items(), key=lambda kv: -kv[1])[:5])
        pack_rows.append((a.get("title") or name, holds, b.get("sessions", 0), b.get("people", 0), top))
    dead = [(r["name"], f"{r['known'][0].get('repo', '')}/{r['known'][0].get('path', '')}", r["known"][0].get("owner", ""),
             r["known"][0].get("last_changed", "")) for r in unused]
    dead_html = "".join(f"<tr><td><b>{esc(a)}</b></td><td>{esc(b)}</td><td>{esc(c)}</td><td>{esc(d)}</td></tr>" for a, b, c, d in dead)
    page = f"""<!doctype html><html lang="{esc(lang)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(T['title'])}</title><style>{CSS}</style></head><body><main>
<h1>{esc(T['title'])}</h1><p class=mute>{esc(intro)}</p>
<div class=tiles>{''.join(f'<div class=tile><b>{esc(v)}</b><span class=mute>{esc(k)}</span></div>' for k, v in tiles)}</div>
<h2>{esc(T['in_use'])}</h2><p class=mute>{esc(help_)}</p>
<div class=wrap><table><tr>{''.join(f'<th{" class=n" if c not in ("skill", "status", "owner", "notes") else ""}>{esc(label(c))}</th>' for c in cols)}</tr>
{''.join(body) or f'<tr><td colspan=12 class=mute>{esc(T["none_yet"])}</td></tr>'}</table></div>
<h2>{esc(T['packs'])}</h2><p class=mute>{esc(T['packs_help'])}</p>
<div class=wrap><table><tr><th>{esc(T['pack'])}</th><th>{esc(T['items'])}</th><th class=n>{esc(T['sessions'])}</th><th class=n>{esc(T['people'])}</th><th>{esc(T['top'])}</th></tr>
{''.join(f'<tr><td><b>{esc(a)}</b></td><td>{esc(b)}</td><td class=n>{c}</td><td class=n>{d}</td><td>{esc(e)}</td></tr>' for a, b, c, d, e in pack_rows) or f'<tr><td colspan=5 class=mute>{esc(T["no_packs"])}</td></tr>'}</table></div>
<h2>{esc(T['dead'].format(days=days))}</h2><p class=mute>{esc(T['dead_help'])}</p>
<div class=wrap><table><tr><th>{esc(T['skill'])}</th><th>{esc(T['where'])}</th><th>{esc(T['owner'])}</th><th>{esc(T['changed'])}</th></tr>
{dead_html or f'<tr><td colspan=4 class=mute>{esc(T["none"])}</td></tr>'}</table></div>
</main></body></html>
"""
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    lines = [f"# {md(T['title'])}", "", md(intro), "", " · ".join(f"**{v}** {md(k)}" for k, v in tiles), "",
             f"## {md(T['in_use'])}", "", md(help_), ""]
    if mdrows:
        lines += ["| " + " | ".join(md(label(c)) for c in cols) + " |", "|" + "---|" * len(cols), *mdrows]
    else:
        lines.append(md(T["none_yet"]))
    lines += ["", f"## {md(T['packs'])}", "", md(T["packs_help"]), ""]
    if pack_rows:
        lines += [f"| {md(T['pack'])} | {md(T['items'])} | {md(T['sessions'])} | {md(T['people'])} | {md(T['top'])} |", "|---|---|---|---|---|",
                  *(f"| **{md(a)}** | {md(b)} | {c} | {d} | {md(e)} |" for a, b, c, d, e in pack_rows)]
    else:
        lines.append(md(T["no_packs"]))
    lines += ["", f"## {md(T['dead'].format(days=days))}", "", md(T["dead_help"]), ""]
    if dead:
        lines += [f"| {md(T['skill'])} | {md(T['where'])} | {md(T['owner'])} | {md(T['changed'])} |", "|---|---|---|---|",
                  *(f"| {md(a)} | {md(b)} | {md(c)} | {md(d)} |" for a, b, c, d in dead)]
    else:
        lines.append(md(T["none"]))
    (ROOT / "DASHBOARD.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out / "index.html"


# ------------------------------------------------------------------ other repositories' skills

def fetch(repos: list[str]) -> list[Path]:
    """Clone (or update) each repository named in hub.json into .scan/<owner>/<repo>, without file contents
    beyond what the scan reads. In the workflow, HUB_READ_TOKEN gives read access to private repositories."""
    import base64
    import os
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    token = os.environ.get("HUB_READ_TOKEN", "")
    auth = ["-c", "http.extraHeader=Authorization: Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()] if token else []
    out = []
    for r in repos:
        dest = ROOT / ".scan" / r
        url = r if (":" in r or r.startswith("/")) else f"{server}/{r}.git"
        if (dest / ".git").is_dir():
            ok = subprocess.run(["git", *auth, "pull", "-q", "--ff-only"], cwd=dest, capture_output=True).returncode == 0
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            ok = subprocess.run(["git", *auth, "clone", "-q", "--filter=blob:none", url, str(dest)], capture_output=True).returncode == 0
        if ok:
            out.append(dest)
        else:
            print(f"could not read {r}")
    return out


def keep_unreadable(new: dict, repos: list[str], read: list[Path]) -> dict:
    """Someone who cannot read one of hub.json's repositories must not wipe its skills from the inventory:
    keep the previous entries of every repository this run could not read."""
    missed = {r for r in repos if ROOT / ".scan" / r not in read}
    try:
        old = json.loads((ROOT / "data" / "inventory.json").read_text(encoding="utf-8")).get("skills", [])
    except (OSError, ValueError):
        old = []
    new["skills"] += [s for s in old if s.get("repo") in missed]
    return new


def settings() -> dict:
    f = ROOT / "hub.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    except ValueError:
        return {}


def main(argv=None) -> None:
    global ROOT
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".", help="the hub repository (default: here)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("inventory", "aggregate", "dashboard", "all"):
        p = sub.add_parser(name)
        p.add_argument("--scan", nargs="*", default=None, help="checkouts to scan besides hub.json's repos")
        p.add_argument("--days", type=int, default=None)
        p.add_argument("--out", default="site")
    args = ap.parse_args(argv)
    ROOT = Path(args.root).resolve()
    cfg = settings()
    days = args.days or int(cfg.get("days") or DEAD_AFTER_DAYS)
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    if args.cmd in ("inventory", "all"):
        repos = cfg.get("repos") or []
        read = fetch(repos)
        inv = keep_unreadable(inventory([Path(p).resolve() for p in (args.scan or [])] + read), repos, read)
        (data / "inventory.json").write_text(json.dumps(inv, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if args.cmd in ("aggregate", "all"):
        (data / "stats.json").write_text(json.dumps(aggregate(days), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if args.cmd in ("dashboard", "all"):
        inv = json.loads((data / "inventory.json").read_text()) if (data / "inventory.json").is_file() else {}
        stats = json.loads((data / "stats.json").read_text()) if (data / "stats.json").is_file() else {}
        print(dashboard(inv, stats, ROOT / args.out, cfg.get("lang", "en")))


if __name__ == "__main__":
    main()
