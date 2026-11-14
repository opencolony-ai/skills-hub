"""Cases: a person's own test of a skill, saved from a real session, and how an answer is graded against it.

A case is a folder:  case.json   the request, the skill, the checks, who made it and from which version
                     answer.md   the answer the person got when they saved it (what they were unhappy with,
                                 or happy with)
                     files/      optional: files the request needs; copied next to the run

Checks (all must hold for the case to pass):
  {"contains": "text"}  {"not_contains": "text"}  {"regex": "pattern"}  in the answer or the files it wrote
  {"file": "glob"}      a file the run wrote
  {"pptx": {"slides_min": 8, "slides_max": 12, "charts_min": 1, "notes": true, "in_bounds": true, "text": "Q3"}}
                        a presentation the run wrote: its slides, native charts, notes, shapes inside the slide, text
  (always, without a check: a presentation a run writes must be a sound file with no empty slide and nothing
   outside the slide)
  {"judge": "question"} a yes/no question about the answer, answered by a model (unknown when none is available)

Programs grade what they can; a model only answers the yes/no questions people wrote. Standard library only.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import json
import re
import shutil
from pathlib import Path

import common
import office

TEXT_FILES = {".md", ".txt", ".csv", ".json", ".html", ".htm", ".xml", ".yaml", ".yml", ".py", ".js", ".ts", ".tsv"}
OWN_COMMANDS = ("/save-as-case", "/skill-feedback", "/skill-eval", "/opencolony-skills:")


# ------------------------------------------------------------------ saving a case from the session

def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


BUILT_IN_TOOLS = {"Bash", "Write", "Edit", "Read", "Glob", "Grep", "NotebookEdit"}


def last_exchange(transcript: str) -> tuple[str, str, str]:
    """(request, how it was asked, answer) of the last exchange in a Claude Code transcript. A typed
    /skill args is returned as (args, "typed:<skill>", answer)."""
    return last_exchange_full(transcript)[:3]


def last_exchange_full(transcript: str) -> tuple[str, str, str, list[str]]:
    """Like last_exchange, and the built-in tools Claude used to answer (a deck needs Bash and Write)."""
    return last_exchange_timed(transcript)[:4]


def last_exchange_timed(transcript: str) -> tuple[str, str, str, list[str], float]:
    """Like last_exchange_full, and when the request was made (seconds since 1970; 0 when unknown)."""
    entries = []
    try:
        with open(transcript, encoding="utf-8") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    pass
    except (OSError, TypeError):
        return "", "", "", [], 0.0
    req_i, request, via = -1, "", "auto"
    for i, e in enumerate(entries):
        m = e.get("message")
        if e.get("type") != "user" or not isinstance(m, dict) or e.get("isMeta"):
            continue
        content = m.get("content")
        if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            continue
        t = _text(content).strip()
        if not t or t.startswith(OWN_COMMANDS) or "<local-command" in t:
            continue
        name = re.search(r"<command-name>/?([^<]+)</command-name>", t)
        if name:
            if name.group(1).startswith(tuple(c.lstrip("/") for c in OWN_COMMANDS)):
                continue
            args = re.search(r"<command-args>(.*?)</command-args>", t, re.S)
            request, via = (args.group(1).strip() if args else ""), f"typed:{name.group(1).strip()}"
        else:
            request, via = t, "auto"
        req_i = i
    if req_i < 0:
        return "", "", "", [], 0.0
    when = 0.0
    try:
        when = dt.datetime.fromisoformat(str(entries[req_i].get("timestamp", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    answer, tools = [], []
    for e in entries[req_i + 1:]:
        m = e.get("message")
        if e.get("type") == "assistant" and isinstance(m, dict):
            t = _text(m.get("content")).strip()
            if t:
                answer.append(t)
            for c in m.get("content") if isinstance(m.get("content"), list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in BUILT_IN_TOOLS and c["name"] not in tools:
                    tools.append(c["name"])
    return request, via, "\n\n".join(answer), tools, when


def cases_dir(cwd: str) -> Path:
    root = common.project_root(cwd)
    return root / ".claude" / "cases" if (root / ".claude").is_dir() else common.home() / "cases"


def save(transcript: str, skill: str, note: str, cwd: str, rating: str = "") -> tuple[Path | None, str]:
    """Save the last exchange as a case for `skill`. The note (what a good answer must have) becomes its
    first check, a yes/no question, exactly as the person wrote it."""
    request, via, answer, tools, when = last_exchange_timed(transcript)
    if not request and not via.startswith("typed:"):
        return None, "nothing to save yet"
    if via.startswith("typed:"):
        skill = skill or via.split(":", 1)[1]
    short = (skill or "no-skill").lstrip("/@").split(":")[-1]
    base = cases_dir(cwd) / short
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    d = base / stamp
    n = 1
    while d.exists():
        n += 1
        d = base / f"{stamp}-{n}"
    d.mkdir(parents=True)
    kind = "command" if skill.startswith("/") else "agent" if skill.startswith("@") else "skill"
    version = common.find_item(kind, skill.lstrip("/@"), cwd)[0] if skill else ""
    case = {"schema": 1, "skill": skill, "request": request, "via": via, "checks": [{"judge": note}] if note else [],
            "made_by": common.user_handle(), "made_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "from_version": version, "answer_was": rating, **({"tools": tools} if tools else {})}
    (d / "case.json").write_text(json.dumps(case, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (d / "answer.md").write_text(answer + "\n", encoding="utf-8")
    given = named_files(request, cwd)
    for f in given:                                     # the files the request names, so the case can run again
        dest = d / "files" / f.relative_to(common.project_root(cwd))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
    if when:                                            # what that session made: the evidence for the person's rating
        root, total = common.project_root(cwd), 0
        for f in written_paths(root, since=when - 1):
            if f in given:                              # (.claude/ is never listed: the cases are not copied into themselves)
                continue
            total += f.stat().st_size
            if total <= 20 * 1024 * 1024:
                dest = d / "produced" / f.relative_to(root)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
    return d, ""


FILE_NAME = re.compile(r"[\w\-./\u3400-\u9fff]+\.(?:txt|md|csv|tsv|json|xlsx|xls|docx|pptx|pdf|html|xml|yaml|yml)\b", re.I)


def named_files(request: str, cwd: str, limit: int = 20 * 1024 * 1024) -> list[Path]:
    """Files in the project that the request names (meeting.txt, data/sales.xlsx), up to 20 MB in all."""
    root = common.project_root(cwd)
    out, total = [], 0
    for name in dict.fromkeys(FILE_NAME.findall(request or "")):
        for base in (Path(cwd or root), root):
            f = (base / name).resolve()
            if f.is_file() and root in f.parents and ".claude" not in f.relative_to(root).parts:
                total += f.stat().st_size
                if total <= limit:
                    out.append(f)
                break
    return out


def load(d: Path) -> dict:
    c = json.loads((d / "case.json").read_text(encoding="utf-8"))
    c["dir"] = d
    c["id"] = f"{d.parent.name}/{d.name}"
    return c


def find(skill: str, cwd: str, extra: list[Path] = ()) -> list[dict]:
    """The cases for a skill: the project's, this person's, and the ones shared in the hub."""
    short = skill.lstrip("/@").split(":")[-1]
    roots = [cases_dir(cwd), common.home() / "cases", common.home() / "hub" / "cases", *extra]
    out, seen = [], set()
    for r in roots:
        for f in sorted((r / short).glob("*/case.json")):
            if f.resolve() in seen:
                continue
            seen.add(f.resolve())
            try:
                out.append(load(f.parent))
            except (OSError, ValueError):
                pass
    return out


# ------------------------------------------------------------------ grading

SKIP_DIRS = {".claude", "node_modules", ".git", "__pycache__"}
MAX_READ = 5 * 1024 * 1024


def written_paths(workdir: Path | None, since: float = 0) -> list[Path]:
    if not workdir or not workdir.is_dir():
        return []
    return [f for f in sorted(workdir.rglob("*")) if f.is_file() and not SKIP_DIRS & set(f.relative_to(workdir).parts)
            and f.stat().st_mtime >= since]


def written_text(workdir: Path | None, since: float = 0) -> dict[str, str]:
    """{path: text} of the files a run wrote; the text of a Word, Excel or PowerPoint file is read out of it."""
    out = {}
    for f in written_paths(workdir, since):
        rel = f.relative_to(workdir).as_posix()
        if f.stat().st_size > MAX_READ:
            out[rel] = ""
        elif f.suffix.lower() in TEXT_FILES:
            out[rel] = f.read_text(encoding="utf-8", errors="replace")
        else:
            out[rel] = office.text_of(f) or ""
    return out


def pptx_reports(workdir: Path | None, since: float = 0) -> dict[str, dict]:
    return {f.relative_to(workdir).as_posix(): office.pptx_report(f) for f in written_paths(workdir, since)
            if f.suffix.lower() == ".pptx"}


def guards(reports: dict[str, dict]) -> list[dict]:
    """Checks every run is held to without anyone writing them: a presentation it wrote must be a sound file with
    nothing outside the slide and no empty slide."""
    out = []
    for path, r in reports.items():
        problems = list(r["problems"]) + ([f"empty slide(s) {r['empty_slides']}"] if r["empty_slides"] else [])
        out.append({"guard": f"{path} is a sound presentation", "ok": not problems, "problems": problems})
    return out


def pptx_ok(spec: dict, r: dict) -> bool:
    """One presentation against a check like {"slides_min": 8, "slides_max": 12, "charts_min": 1, "notes": true,
    "in_bounds": true, "text": "Q3"}; every key given must hold."""
    return (r["opens"]
            and r["slides"] >= int(spec.get("slides_min", 0)) and r["slides"] <= int(spec.get("slides_max", 10 ** 6))
            and r["charts"] >= int(spec.get("charts_min", 0)) and r["pictures"] >= int(spec.get("pictures_min", 0))
            and (not spec.get("notes") or r["slides_with_notes"] >= r["slides"] > 0)
            and (not spec.get("in_bounds") or not r["out_of_bounds"])
            and (not spec.get("text") or str(spec["text"]).lower() in "\n".join(r["text"]).lower()))


def grade(checks: list[dict], answer: str, files: dict[str, str], judge=None, reports: dict | None = None) -> list[dict]:
    """One result per check: True, False, or None (could not tell: no judge available)."""
    haystack = answer + "\n" + "\n".join(files.values())
    out = []
    for c in checks:
        if "contains" in c:
            ok = str(c["contains"]).lower() in haystack.lower()
        elif "not_contains" in c:
            ok = str(c["not_contains"]).lower() not in haystack.lower()
        elif "regex" in c:
            try:
                ok = re.search(c["regex"], haystack, re.M) is not None
            except re.error:
                ok = None
        elif "file" in c:
            ok = any(fnmatch.fnmatch(p, c["file"]) for p in files)
        elif "pptx" in c:
            ok = any(pptx_ok(c["pptx"], r) for r in (reports or {}).values())
        elif "judge" in c:
            ok = judge(c["judge"], answer, files) if judge else None
        else:
            ok = None
        out.append({**c, "ok": ok})
    return out


def passed(results: list[dict]) -> bool | None:
    if any(r["ok"] is False for r in results):
        return False
    if any(r["ok"] is None for r in results) or not results:
        return None
    return True


def weak_checks(case: dict, judge=None) -> list[str]:
    """Problems with a case's checks, found by grading answers whose outcome is known: an empty answer must fail;
    the saved answer must fail if the person rated it 👎 in that session, and pass if they rated it 👍."""
    problems = []
    if not case.get("checks"):
        return ["no checks: say what a good answer must have"]
    if not any("judge" in c for c in case["checks"]) and passed(grade(case["checks"], "", {})) is True:
        problems.append("an empty answer passes: add a check that needs something in the answer")
    saved = case.get("dir") and (case["dir"] / "answer.md")
    if case.get("answer_was") in ("up", "down") and saved and saved.is_file():
        made = case["dir"] / "produced"
        r = passed(grade(case["checks"], saved.read_text(encoding="utf-8"), written_text(made), judge=judge,
                         reports=pptx_reports(made)))
        if case["answer_was"] == "down" and r is True:
            problems.append("the answer you were unhappy with passes every check: the checks miss what was wrong")
        if case["answer_was"] == "up" and r is False:
            problems.append("the answer you were happy with fails a check: a check asks for more than you needed")
    return problems
