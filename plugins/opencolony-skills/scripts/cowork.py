#!/usr/bin/env python3
"""Microsoft 365 Copilot Cowork: the same hub, without hooks.

Cowork reads a person's own skills from their OneDrive (`Documents/Cowork/skills/<name>/SKILL.md`) at the start of
each session, and has no hooks. So, from this computer, through the folder the OneDrive app keeps in sync:

    cowork.py install PACK     put a pack's skills where Cowork reads them (and remember the pack)
    cowork.py sync             bring them up to date from the hub, and collect what Cowork reported
    cowork.py status           what is installed, what was collected
    cowork.py remove           take out the skills this program put there (others are never touched)

Usage and feedback: each skill gets a short "report back" section at its end, asking Cowork to save a small text
file after it used the skill (the skill, its version, how it went, the person's own words if they said any).
`sync` reads those files, counts them like any other use, sends the words to the skill's owner, and moves the files
away. That part depends on Cowork following the instruction, and has not been tried in a real tenant.

Cowork's own limits are checked before anything is written: a skill's name must match its folder, at most 20
files beside SKILL.md and 10 MB in all, at most 50 skills. A skill that does not fit is reported, not installed.
A skill someone changed in OneDrive is left alone. Standard library only; works on Windows and macOS.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import pull  # noqa: E402

MAX_SKILLS, MAX_FILES, MAX_FILE, MAX_TOTAL, MAX_SKILL_MD = 50, 20, 5 * 1024 * 1024, 10 * 1024 * 1024, 1024 * 1024
MARK = "<!-- opencolony:report-back -->"
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REPORT = re.compile(r"^oc-([a-z0-9-]+)-(\d{8}-\d{6})[\w-]*\.txt$")
FOOTER = """

{mark}
## Report back (added by OpenColony; version {version})

When you have finished a task in which you used this skill, create one small text file in the Cowork output folder
named `oc-{name}-<date and time as YYYYMMDD-HHMMSS>.txt` with exactly these five lines:

skill: {name}
version: {version}
result: done, partly or failed
rating: up or down, only if the person said whether this skill's result was good or not; otherwise leave empty
feedback: the person's own words about this skill's result in this task, if they said any; otherwise leave empty

Do it once per task. Then tell the person in one short line that a usage note for the skill's owner was saved.
Never put anything else from the task in the file: no file contents, no names of people or clients, no numbers.
"""


def state_file() -> Path:
    return common.home() / "cowork.json"


def state() -> dict:
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(s: dict) -> None:
    common.home().mkdir(parents=True, exist_ok=True)
    state_file().write_text(json.dumps(s, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def skills_dir() -> Path | None:
    od = common.onedrive_dir()
    if not od:
        return None
    base = od / "Documents" / "Cowork"
    for name in ("skills", "Skills"):
        if (base / name).is_dir():
            return base / name
    return base / "skills"


def report_dirs() -> list[Path]:
    """Where Cowork may have saved its small files: its output folder(s) in OneDrive, or the one named in config."""
    od = common.onedrive_dir()
    out = [Path(p) for p in [common.config().get("cowork_output")] if p]
    if od:
        out += [od / "Cowork", od / "Documents" / "Cowork"]
    return [p for p in out if p.is_dir()]


def fits(folder: Path, name: str) -> list[str]:
    """Why Cowork would not take this skill, or []."""
    problems = []
    if not NAME.match(name) or len(name) > 64:
        problems.append("its name must be lower-case words joined by hyphens, at most 64 characters")
    files = [f for f in folder.rglob("*") if f.is_file() and "__pycache__" not in f.parts and f.name != "PROVENANCE.json"]
    others = [f for f in files if f.name != "SKILL.md" or f.parent != folder]
    if len(others) > MAX_FILES:
        problems.append(f"{len(others)} files beside SKILL.md (Cowork allows {MAX_FILES})")
    if any(f.stat().st_size > MAX_FILE for f in others):
        problems.append("a file is over 5 MB")
    if sum(f.stat().st_size for f in others) > MAX_TOTAL:
        problems.append("its files are over 10 MB in all")
    if (folder / "SKILL.md").stat().st_size > MAX_SKILL_MD - 4000:
        problems.append("its SKILL.md is over 1 MB")
    if any(f.name.startswith(".") for f in others):
        problems.append("it has hidden files")
    return problems


def cowork_skill_md(text: str, name: str, version: str) -> str:
    """The skill as Cowork gets it: the hub's text, its `name` set to the folder's, and the report-back section."""
    text = text.split(MARK)[0].rstrip()
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if m:
        head = m.group(1)
        head = re.sub(r"(?m)^name:.*$", f"name: {name}", head) if re.search(r"(?m)^name:", head) else f"name: {name}\n" + head
        text = "---\n" + head + "\n---" + text[m.end():]
    else:
        text = f"---\nname: {name}\ndescription: {name}\n---\n\n" + text
    return text + FOOTER.format(mark=MARK, name=name, version=version)


def install(hub: Path, pack_name: str) -> dict:
    """Put (or refresh) a pack's skills in the person's OneDrive Cowork folder."""
    dest = skills_dir()
    report = {"pack": pack_name, "added": [], "updated": [], "unchanged": [], "removed": [], "kept_changes_here": [],
              "does_not_fit": [], "not_for_cowork": [], "folder": str(dest) if dest else ""}
    if dest is None:
        raise SystemExit("no OneDrive folder on this computer: install the OneDrive app and sign in, or set \"onedrive\" in the settings")
    f = hub / "packs" / f"{pack_name}.json"
    if not f.is_file():
        raise SystemExit(f"no pack {pack_name!r} in the hub")
    items, _ = pull.resolve(hub, json.loads(f.read_text(encoding="utf-8")))
    report["not_for_cowork"] = [f"{i['type']} {i['name']}" for i in items if i["type"] != "skill"]   # Cowork has no commands or subagents
    st = state()
    before = {i["name"]: i for i in st.get("items", [])}
    dest.mkdir(parents=True, exist_ok=True)
    mine = set(before)
    others = sum(1 for d in dest.iterdir() if d.is_dir() and d.name not in mine)
    entries = []
    for it in [i for i in items if i["type"] == "skill"]:
        name, src, target = it["name"], hub / it["from"], dest / it["name"]
        prev = before.pop(name, None)
        here = pull.tree(target) if target.exists() else ""
        if here and (prev is None or here != prev.get("tree")):
            report["kept_changes_here"].append(name)
            if prev:
                entries.append(prev)
            continue
        problems = fits(src, name)
        if not prev and others + len(entries) >= MAX_SKILLS:
            problems.append(f"Cowork allows {MAX_SKILLS} custom skills and this account is at the limit")
        if problems:
            report["does_not_fit"].append({"name": name, "why": problems})
            if prev:
                entries.append(prev)
            continue
        version = common.digest(src / "SKILL.md")
        if prev and prev.get("version") == version and prev.get("src_tree") == pull.tree(src):
            report["unchanged"].append(name)
            entries.append(prev)
            continue
        pull.place(src, target)
        (target / "PROVENANCE.json").unlink(missing_ok=True)
        (target / "SKILL.md").write_text(cowork_skill_md((src / "SKILL.md").read_text(encoding="utf-8", errors="replace"), name, version),
                                         encoding="utf-8")
        report["updated" if prev else "added"].append(name)
        entries.append({"name": name, "version": version, "src_tree": pull.tree(src), "tree": pull.tree(target)})
    for name, prev in before.items():                   # no longer in the pack
        target = dest / name
        if target.exists() and pull.tree(target) != prev.get("tree"):
            report["kept_changes_here"].append(name)
            entries.append(prev)
            continue
        if target.is_dir():
            shutil.rmtree(target)
        report["removed"].append(name)
    save_state({**st, "pack": pack_name, "items": sorted(entries, key=lambda e: e["name"]),
                "installed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "folder": str(dest)})
    return report


def remove() -> dict:
    dest, st = skills_dir(), state()
    removed, kept = [], []
    for it in st.get("items", []):
        target = dest / it["name"] if dest else None
        if target and target.is_dir():
            if pull.tree(target) != it.get("tree"):
                kept.append(it["name"])
                continue
            shutil.rmtree(target)
        removed.append(it["name"])
    save_state({k: v for k, v in st.items() if k not in ("pack", "items")})
    return {"removed": removed, "kept_changes_here": kept}


def parse_report(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip().lower() in ("skill", "version", "result", "rating", "feedback"):
            out[k.strip().lower()] = v.strip()
    return out


def collect(send=None) -> dict:
    """Read the small files Cowork saved, record them as uses and feedback, and move them away.
    `send(skill, rating, note)` sends a sentence to the owner (suggest.quick by default)."""
    st = state()
    known = {i["name"]: i for i in st.get("items", [])}
    seen = set(st.get("seen", []))
    out = {"uses": 0, "feedback": 0, "ignored": 0, "sent": []}
    if send is None:
        import suggest
        send = lambda skill, rating, note: suggest.quick(skill, rating, note, "", client="cowork")      # noqa: E731
    for base in report_dirs():
        for f in sorted(base.rglob("oc-*.txt")):
            m = REPORT.match(f.name)
            if not m or "opencolony" in f.relative_to(base).parts or f.name in seen:
                continue
            try:
                r = parse_report(f.read_text(encoding="utf-8", errors="replace")[:4000])
            except OSError:
                continue
            name = r.get("skill") or m.group(1)
            if name not in known:
                out["ignored"] += 1
                continue
            try:
                when = dt.datetime.strptime(m.group(2), "%Y%m%d-%H%M%S")
                if abs((when - dt.datetime.now()).total_seconds()) > 2 * 86400 * 365:
                    raise ValueError
            except ValueError:
                when = dt.datetime.fromtimestamp(f.stat().st_mtime)
            sid = f"cowork-{f.stem}"
            common.append({"kind": "use", "session": sid, "skill": name, "type": "skill", "version": r.get("version") or known[name]["version"],
                           "source": "cowork", "pack": st.get("pack", ""), "client": "cowork",
                           "result": r.get("result", "")[:20]}, now=when)
            out["uses"] += 1
            words = " ".join(r.get("feedback", "").split())[:500]
            rating = r.get("rating", "").lower()
            rating = rating if rating in ("up", "down") else ("down" if r.get("result", "").lower().startswith(("fail", "part")) else "")
            if words or rating:
                rating = rating or "down"
                common.append({"kind": "feedback", "session": sid, "skill": name, "rating": rating, "note": words, "from": "cowork"}, now=when)
                out["feedback"] += 1
                if words:
                    try:
                        sent = send(name, rating, words)
                        if sent.get("sent"):
                            out["sent"].append(sent.get("issue", ""))
                    except Exception:                 # noqa: BLE001 - it is recorded; the weekly usage still carries it
                        pass
            seen.add(f.name)
            done = base / "opencolony" / "processed"
            try:
                done.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(done / f.name))
            except OSError:
                pass
    for base in report_dirs():                         # processed files are kept a month, then go
        for f in (base / "opencolony" / "processed").glob("oc-*.txt") if (base / "opencolony" / "processed").is_dir() else []:
            if time.time() - f.stat().st_mtime > 30 * 86400:
                f.unlink(missing_ok=True)
    save_state({**state(), "seen": sorted(seen)[-2000:], "collected_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "collected": {"uses": st.get("collected", {}).get("uses", 0) + out["uses"],
                              "feedback": st.get("collected", {}).get("feedback", 0) + out["feedback"]}})
    return out


def sync(hub_url: str = "") -> dict:
    """Update the installed pack from the hub, then collect. Nothing to do when no pack was installed for Cowork."""
    st = state()
    if not st.get("pack"):
        return {"configured": False}
    import update
    hub_url = hub_url or common.config().get("hub") or ""
    clone = update.refresh(hub_url) if hub_url else None
    out = {"configured": True}
    if clone is not None and (clone / "packs" / f"{st['pack']}.json").is_file():
        out["install"] = install(clone, st["pack"])
    out["collect"] = collect()
    return out


def status() -> dict:
    st, dest = state(), skills_dir()
    return {"onedrive": str(common.onedrive_dir() or ""), "folder": str(dest) if dest else "", "pack": st.get("pack", ""),
            "skills": [i["name"] for i in st.get("items", [])], "installed_at": st.get("installed_at", ""),
            "collected_at": st.get("collected_at", ""), "collected": st.get("collected", {"uses": 0, "feedback": 0})}


def main(argv=None) -> None:
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a[0] if a else "status"
    if cmd == "install":
        if len(a) < 2:
            raise SystemExit("which pack? cowork.py install PACK")
        import update
        clone = update.refresh(common.config().get("hub") or "") if common.config().get("hub") else None
        hub = clone or pull.find_hub(Path(__file__).resolve().parent)
        out = install(hub, a[1])
    elif cmd == "sync":
        out = sync()
    elif cmd == "remove":
        out = remove()
    else:
        out = status()
    common.write_stdout_json(out)


if __name__ == "__main__":
    main()
