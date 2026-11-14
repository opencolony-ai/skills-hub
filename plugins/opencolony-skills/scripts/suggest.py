#!/usr/bin/env python3
"""Suggestions: what a person says about a skill, sent to its owner as a GitHub issue, with a de-identified
copy of their case (files included) in their own OneDrive when there is one.

The skill-suggestion skill shows the suggestion in the conversation, as blocks the person reads and this program
parses from the session's transcript (Claude itself writes no file: Claude Code guards .claude/):

    ~~~~opencolony-suggestion
    skill: meeting-notes
    kind: problem
    title: ...
    from_case: .claude/cases/meeting-notes/<id>      (only with a copy of a case)
    ---
    the text
    ~~~~
    ~~~~opencolony-file case/files/meeting.txt        (one block per file of the de-identified copy)
    ...
    ~~~~

When the person types /send-suggestion, the hook writes the latest draft to .claude/suggestions/<id>/
(suggestion.md, case/), and then:

Programs then decide what may leave (Claude only writes the copy):
  check   the copy against the original case: identifiers that survived (e-mail addresses, phone numbers,
          numbers of 4+ digits, web addresses, codes) block sending; other words that survived are listed for
          the person to look at; files that cannot be read (PDF, images) and files over the size limits block.
  send    (after the person typed /send-suggestion) the copy must still fail the case's checks with the current
          version, or it would not show the problem; then the case goes to the person's OneDrive and the text to
          an issue in the owner's repository (the skill's `feedback:`, else the hub's, else the hub itself).

    suggest.py list   [--project DIR]
    suggest.py check  DIR
    suggest.py send   DIR [--no-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cases  # noqa: E402
import common  # noqa: E402
import office  # noqa: E402

MAX_FILE = 10 * 1024 * 1024
MAX_CASE = 20 * 1024 * 1024
HARD = [("e-mail", r"[\w.+-]+@[\w-]+\.[\w.-]+"), ("web address", r"https?://[^\s)>\]]+"),
        ("phone", r"(?<!\d)\+?\d[\d\s-]{7,}\d(?!\d)"), ("number", r"(?<![\d.])\d{1,3}(?:[,.]\d{3})+(?:\.\d+)?(?![\d])|(?<![\d.])\d{4,}(?![\d])"),
        ("code", r"\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9-]{6,}\b")]
NAMEISH = re.compile(r"\b[A-Z][a-z]{2,}(?:\s[A-Z][a-z]{2,})*\b")
COMMON = set("""營收 毛利率 毛利 流失 流失數 淨增 新增 客戶數 確認 變化 檔案 本季重點 重點 總數 趨勢 建議 內容 結構 版本 版面 圖表 標題 投影片 簡報 講者備註
未定 待定 待指派 待確認 無 不明 請整理 整理 會議 紀錄 會議紀錄 逐字稿 負責 負責人 電話 地址 期限 日期 客戶 專案 報告 設計 設計稿 測試 新聞稿 上線
預算 訂單 金額 項目 待辦 決議 問題 備註 說明 範例 例如 今天 明天 下週 週一 週二 週三 週四 週五 週六 週日 部門 公司 經理 主管 同事
What Example Owner Task Due Date Title Notes Summary Action Items Decisions Open Questions Client Person""".split())


# ------------------------------------------------------------------ reading text out of files

def text_of(f: Path) -> str | None:
    """The text in a file, or None when it cannot be read (and so cannot be checked)."""
    if f.suffix.lower() in cases.TEXT_FILES:
        return f.read_text(encoding="utf-8", errors="replace")
    return office.text_of(f)


META = {"schema", "via", "made_by", "made_at", "from_version", "answer_was", "skill", "tools"}


def case_json_text(t: str) -> str:
    """Only what people wrote in a case.json (the request, the checks), not its bookkeeping."""
    try:
        c = json.loads(t)
    except ValueError:
        return t
    return "\n".join(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v for k, v in c.items() if k not in META)


def texts(d: Path) -> tuple[dict[str, str], list[str]]:
    """{relative path: text} for every file under d, and the files that cannot be read."""
    out, unreadable = {}, []
    if d.is_file():
        files, base = [d], d.parent
    else:
        files, base = sorted(f for f in d.rglob("*") if f.is_file()), d
    for f in files:
        t = text_of(f)
        if t is not None and f.name == "case.json":
            t = case_json_text(t)
        if t is None:
            unreadable.append(f.relative_to(base).as_posix())
        else:
            out[f.relative_to(base).as_posix()] = t
    return out, unreadable


# ------------------------------------------------------------------ check

# formats that look like codes but only say what kind of data this is: 2026Q3, FY2026, 2026-11, 11/20, v1.2.3, A1:B7
FORMAT = re.compile(r"^(?:(?:19|20)\d\d[-_ ]?(?:Q[1-4]|H[12]|W\d{1,2}|M\d{1,2})|FY\d{2,4}|Q[1-4][-_ ]?(?:19|20)\d\d|"
                    r"(?:19|20)\d\d-\d\d(?:-\d\d)?|v?\d+(?:\.\d+){1,3}|[A-Z]{1,2}\d{1,4}(?::[A-Z]{1,2}\d{1,4})?|"
                    r"UTF-?8|ISO-?\d+|SHA-?\d+|MD5|[A-Za-z]+-?\d{1,3})$", re.I)


DATE = re.compile(r"(?<!\d)(?:19|20)\d\d[-/.]\d{1,2}[-/.]\d{1,2}(?!\d)|(?<!\d)\d{1,2}[-/.]\d{1,2}[-/.](?:19|20)\d\d(?!\d)")


def hard_tokens(text: str) -> dict[str, str]:
    found = {}
    undated = DATE.sub(" ", text)                      # a date is not a phone number
    for kind, pat in HARD:
        for m in re.finditer(pat, undated if kind == "phone" else text):
            tok = m.group(0).strip()
            if kind == "code" and FORMAT.match(tok):
                continue
            norm = re.sub(r"[\s-]", "", tok) if kind in ("phone",) else tok
            if kind == "number" and re.fullmatch(r"(19|20)\d\d", norm):
                continue                    # a year is not an identifier
            found[norm] = kind
    return found


NAME_SPOTS = [re.compile(p, re.M) for p in (
    r"(?<![\u3400-\u9fff])([\u3400-\u9fff]{2,4})\s*[：:]",              # a speaker: 王小明：
    r"\|\s*([\u3400-\u9fff]{2,4})\s*(?=\|)",                            # a short table cell
    r"(?:客戶|廠商|公司|單位|品牌|案主)(?:是|為|：|:)?\s*([\u3400-\u9fff]{2,6})",  # a client: 客戶是台積電
    r"([\u3400-\u9fff]{1,3})(?:先生|小姐|經理|主任|協理|副總|總經理|老師|醫師|律師)")]


def soft_tokens(text: str) -> set[str]:
    """Words where names usually are: capitalised words, a speaker before a colon, a short table cell, the word
    after 客戶 (client), the word before a title (先生, 經理)."""
    out = set(re.findall(r"\b[A-Z][a-z]{2,}(?:\s[A-Z][a-z]{2,})+\b", text))          # John Smith
    out |= set(re.findall(r"(?<=[a-z,;]\s)[A-Z][a-z]{2,}\b", text))                    # a capital in the middle of a sentence
    for p in NAME_SPOTS:
        out |= {m.group(1) for m in p.finditer(text)}
    return {t for t in out if t not in COMMON and t[0] not in "的這那個要不和與及或在把被"}


def front_matter(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load(d: Path) -> dict:
    text = (d / "suggestion.md").read_text(encoding="utf-8")
    fm = front_matter(text)
    body = re.sub(r"^---\s*\n.*?\n---\s*\n?", "", text, count=1, flags=re.S).strip()
    return {"dir": d, "id": d.name, "skill": fm.get("skill", ""), "kind": fm.get("kind", "suggestion"),
            "title": fm.get("title", "") or body.splitlines()[0][:80] if body else fm.get("title", ""),
            "from_case": fm.get("from_case", ""), "body": body}


def check(d: Path, cwd: str = "") -> dict:
    """What would leave, and what must not: written to <dir>/check.json."""
    s = load(d)
    root = common.project_root(cwd or str(d))
    original = (root / s["from_case"]).resolve() if s["from_case"] else None
    shared, unreadable = texts(d / "case") if (d / "case").is_dir() else ({}, [])
    shared["suggestion.md"] = s["body"]
    problems, soft = [], []
    for f in unreadable:
        problems.append(f"cannot check {f}: share a text, Word, Excel or PowerPoint version instead")
    total = 0
    for f in (d / "case").rglob("*") if (d / "case").is_dir() else []:
        if f.is_file():
            total += f.stat().st_size
            if f.stat().st_size > MAX_FILE:
                problems.append(f"{f.relative_to(d).as_posix()} is over 10 MB: keep only the part that shows the problem")
    if total > MAX_CASE:
        problems.append("the case is over 20 MB in all: keep only what shows the problem")
    if original and original.is_dir():
        orig, _ = texts(original)
        for rel in [p for p in shared if p.startswith("files/")]:      # a file the request used from the project
            name = Path(rel).name
            for f in root.rglob(name):
                parts = f.relative_to(root).parts
                if f.is_file() and ".claude" not in parts and ".git" not in parts:
                    t = text_of(f)
                    if t is not None:
                        orig[f"project:{f.relative_to(root).as_posix()}"] = t
        o_text, s_text = "\n".join(orig.values()), "\n".join(shared.values())
        oh, sh = hard_tokens(o_text), hard_tokens(s_text)
        for tok in sorted(set(oh) & set(sh)):
            problems.append(f"still in the copy: {oh[tok]} {tok}")
        allowed = set()
        _, _, skill_file = common.find_item("command" if s["skill"].startswith("/") else "agent" if s["skill"].startswith("@") else "skill",
                                            s["skill"].lstrip("/@"), str(root))
        if skill_file:
            allowed |= soft_tokens(skill_file.read_text(encoding="utf-8", errors="replace"))
        for c in json.loads((original / "case.json").read_text(encoding="utf-8")).get("checks", []) if (original / "case.json").is_file() else []:
            allowed |= soft_tokens(" ".join(str(v) for v in c.values()))
        # a word in a name's place in the original that is still anywhere in the copy
        soft = sorted((t for t in soft_tokens(o_text) - allowed if t in s_text), key=lambda t: (-len(t), t))[:20]
    elif s["from_case"]:
        problems.append(f"the original case {s['from_case']} is not there, so the copy cannot be checked")
    if (d / "case").is_dir() and not s["from_case"]:
        problems.append("the copy does not say which saved case it was made from (from_case), so it cannot be checked")
    result = {"checked_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "problems": problems,
              "look_at": soft, "files": sorted(p for p in shared if p != "suggestion.md")}
    (d / "check.json").write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return result


BLOCK = re.compile(r"^~~~~opencolony-(suggestion|file)[ \t]*([^\n]*)\n(.*?)\n~~~~[ \t]*$", re.S | re.M)


def _assistant_turns(transcript: str) -> list[str]:
    """The text Claude wrote, one string per turn (everything between two of the person's messages)."""
    turns, cur = [], []
    try:
        lines = open(transcript, encoding="utf-8").read().splitlines()
    except (OSError, TypeError):
        return []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        m = e.get("message")
        if not isinstance(m, dict):
            continue
        if e.get("type") == "user" and not e.get("isMeta") and isinstance(m.get("content"), str):
            if cur:
                turns.append("\n".join(cur))
            cur = []
        elif e.get("type") == "assistant":
            t = cases._text(m.get("content"))
            if t:
                cur.append(t)
    if cur:
        turns.append("\n".join(cur))
    return turns


def draft(transcript: str, last_turn_only: bool = False) -> tuple[str, list[tuple[str, str]]] | None:
    """(suggestion block, [(path, content)]) of the latest draft in the conversation."""
    turns = _assistant_turns(transcript)
    for text in reversed(turns[-1:] if last_turn_only else turns):
        blocks = BLOCK.findall(text)
        sugg = [b for b in blocks if b[0] == "suggestion"]
        if sugg:
            return sugg[-1][2], [(b[1].strip(), b[2]) for b in blocks if b[0] == "file"]
    return None


def materialize(transcript: str, cwd: str, last_turn_only: bool = False) -> Path | None:
    """Write the latest draft to .claude/suggestions/<id>/ (the same draft twice is written once)."""
    import hashlib
    got = draft(transcript, last_turn_only)
    if not got:
        return None
    head_body, files = got
    h = hashlib.sha256(json.dumps([head_body, files], ensure_ascii=False).encode()).hexdigest()[:16]
    base = common.project_root(cwd) / ".claude" / "suggestions"
    for d in sorted(base.glob("*")) if base.is_dir() else []:
        try:
            if json.loads((d / "draft.json").read_text(encoding="utf-8")).get("hash") == h:
                return d
        except (OSError, ValueError):
            pass
    head, _, body = head_body.partition("\n---\n")
    fm = front_matter(f"---\n{head}\n---\n")
    short = re.sub(r"[^\w.-]+", "-", fm.get("skill", "general").lstrip("/@").split(":")[-1]) or "general"
    d = base / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{short}-{h[:6]}"
    (d / "case").mkdir(parents=True, exist_ok=True) if files else d.mkdir(parents=True, exist_ok=True)
    (d / "suggestion.md").write_text(f"---\n{head.strip()}\n---\n{body.strip()}\n", encoding="utf-8")
    for path, content in files:
        p = Path(path)
        if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] != "case":
            continue                        # only files of the copy, inside it
        (d / p).parent.mkdir(parents=True, exist_ok=True)
        (d / p).write_text(content + "\n", encoding="utf-8")
    (d / "draft.json").write_text(json.dumps({"hash": h}), encoding="utf-8")
    return d


def pending(cwd: str) -> list[Path]:
    base = common.project_root(cwd) / ".claude" / "suggestions"
    return sorted(d for d in base.glob("*") if (d / "suggestion.md").is_file() and not (d / "sent.json").exists()) if base.is_dir() else []


# ------------------------------------------------------------------ send

def destination(skill: str, cwd: str) -> tuple[str, str, str]:
    """(host, owner, repo) for a skill's feedback: `feedback:` in its own file, else the hub's default, else the hub."""
    kind = "command" if skill.startswith("/") else "agent" if skill.startswith("@") else "skill"
    hub = common.project_lock(cwd).get("hub") or common.config().get("hub") or ""
    _, _, f = common.find_item(kind, skill.lstrip("/@"), cwd)
    spec = front_matter(f.read_text(encoding="utf-8", errors="replace")).get("feedback", "") if f else ""
    if not spec:
        spec = common.project_lock(cwd).get("feedback") or common.config().get("feedback") or ""
        hubjson = common.home() / "hub" / "hub.json"
        if not spec and hubjson.is_file():
            spec = json.loads(hubjson.read_text(encoding="utf-8")).get("feedback", "")
    return common.parse_repo(spec or hub, like=hub)


def open_issue(host: str, owner: str, repo: str, title: str, body: str, labels: list[str]) -> tuple[str, str]:
    """(issue URL, "") or ("", why not)."""
    token = common.github_token(host)
    if not token:
        return "", (f"no saved GitHub login for {host}: sign in once with `gh auth login`, or clone any repository "
                    f"from {host} over HTTPS so git remembers the login")
    req = urllib.request.Request(f"{common.api_base(host)}/repos/{owner}/{repo}/issues", method="POST",
                                 data=json.dumps({"title": title, "body": body, "labels": labels}).encode("utf-8"),
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json", "User-Agent": "opencolony-skills"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8")).get("html_url", ""), ""
    except urllib.error.HTTPError as e:
        why = {401: "the saved GitHub login was refused", 403: "no permission to open issues there",
               404: f"{owner}/{repo} does not exist, or you cannot see it", 410: "issues are turned off there"}.get(e.code, f"GitHub said {e.code}")
        return "", f"{why} ({owner}/{repo})"
    except (urllib.error.URLError, OSError) as e:
        return "", f"cannot reach {host}: {e}"


def to_onedrive(d: Path, short: str) -> tuple[str, str]:
    """Copy the case into the person's OneDrive (its app uploads it). (path or link, "") or ("", why not)."""
    od = common.onedrive_dir()
    if not od:
        return "", "no OneDrive folder on this computer"
    rel = Path("OpenColony") / "cases" / short / f"{d.name}"
    dest = od / rel
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(d / "case", dest)
    web = common.config().get("onedrive_url", "").rstrip("/")
    return (f"{web}/{urllib.parse.quote(rel.as_posix())}" if web else f"OneDrive: {rel.as_posix()}"), ""


def reproduces(d: Path, skill: str, cwd: str) -> bool | None:
    """Does the de-identified copy still fail its checks with the current version? None when it cannot run."""
    import evaluate
    c = cases.load(d / "case")
    if not c.get("checks"):
        return None
    try:
        row = evaluate.run_one(c, skill, common.project_root(cwd), "current")
    except SystemExit:
        return None
    if row.get("error") or "skipped" in row:
        return None
    return row["passed"] is False


def send(d: Path, cwd: str = "", run: bool = True) -> dict:
    s = load(d)
    chk = check(d, cwd)
    if chk["problems"]:
        return {"sent": False, "why": chk["problems"]}
    short = s["skill"].lstrip("/@").split(":")[-1] or "general"
    out = {"sent": False, "issue": "", "files": "", "reproduces": None, "notes": []}
    if (d / "case").is_dir():
        out["reproduces"] = reproduces(d, s["skill"], cwd) if run else None
        if out["reproduces"] is False:
            out["notes"].append("the de-identified copy does not show the problem any more, so its files were not sent")
        else:
            out["files"], why = to_onedrive(d, short)
            if why:
                out["notes"].append(why)
    host, owner, repo = destination(s["skill"], cwd)
    if not owner:
        return {**out, "why": ["nowhere to send it: the skill names no feedback repository and no hub is known"]}
    version = common.find_item("skill", s["skill"].lstrip("/@"), cwd)[0] if s["skill"] else ""
    lock = common.project_lock(cwd)
    case = cases.load(d / "case") if (d / "case").is_dir() else {}
    lines = [s["body"], "", "---",
             f"**Skill**: `{s['skill'] or '-'}`" + (f" (version `{version}`)" if version else "") + (f", pack `{lock['pack']}`" if lock.get("pack") else ""),
             f"**From**: {common.user_handle()}, {dt.date.today().isoformat()}"]
    if case:
        lines.append("**Case checks**: " + "; ".join(next(iter(c.values())) if len(c) == 1 else json.dumps(c, ensure_ascii=False)
                                                    for c in case.get("checks", [])))
        lines.append("**Still fails with the current version**: " + {True: "yes", False: "no", None: "not run"}[out["reproduces"]])
    if out["files"]:
        lines.append(f"**Files** (de-identified): {out['files']} (ask {common.user_handle()} to share the folder if it does not open)")
    lines.append("")
    lines.append("<!-- opencolony " + json.dumps({"schema": 1, "skill": s["skill"], "version": version, "kind": s["kind"],
                                                   "id": s["id"], "files": bool(out["files"])}) + " -->")
    url, why = open_issue(host, owner, repo, f"[{short}] {s['title']}"[:200], "\n".join(lines), [f"skill:{short}", s["kind"]])
    if why:
        return {**out, "why": [why]}
    out.update(sent=True, issue=url)
    (d / "sent.json").write_text(json.dumps({**out, "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")},
                                            ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    common.append({"kind": "suggestion", "skill": s["skill"], "sent": True, "with_files": bool(out["files"])})
    return out


def notice(text: str) -> None:
    common.home().mkdir(parents=True, exist_ok=True)
    with (common.home() / "notices.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"said": text}, ensure_ascii=False) + "\n")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("list"); l.add_argument("--project", default=".")
    c = sub.add_parser("check"); c.add_argument("dir", type=Path)
    s = sub.add_parser("send"); s.add_argument("dir", type=Path); s.add_argument("--no-run", action="store_true")
    s.add_argument("--notify", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        out = [{"id": d.name, **{k: v for k, v in load(d).items() if k in ("skill", "kind", "title")}} for d in pending(a.project)]
    elif a.cmd == "check":
        out = check(a.dir.resolve())
    else:
        out = send(a.dir.resolve(), str(a.dir.resolve()), run=not a.no_run)
        if a.notify:
            zh = common.lang(str(a.dir.resolve())) == "zh-TW"
            if out.get("sent"):
                notice((f"建議已送出：{out['issue']}" if zh else f"Suggestion sent: {out['issue']}") +
                       ((f"（檔案在 {out['files']}，請分享給負責人）" if zh else f" (files: {out['files']}; share that folder with the owner)")
                        if out.get("files") else ""))
            else:
                notice(("建議沒有送出：" if zh else "Suggestion not sent: ") + " ".join(out.get("why", [])))
    common.write_stdout_json(out)


if __name__ == "__main__":
    main()
