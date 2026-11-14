#!/usr/bin/env python3
"""Run people's own cases against a skill: without it, with the current version, and with a proposed one.

    evaluate.py run SKILL [--project DIR] [--arms none,current] [--proposed DIR] [--times 1]
    evaluate.py checks SKILL [--project DIR]       are the checks strong enough? (no Claude runs except judges)
    evaluate.py list SKILL [--project DIR]         the cases there are

Each run is a fresh Claude Code session (`claude -p`) in an empty folder holding only the case's files and the
skill version of that arm, so it uses the person's own Claude quota. What is compared, in this order: how many
runs pass every check, then tokens, then time. The verdict follows fixed rules; a model only answers the
yes/no questions people wrote as checks.

Results stay on this machine in ~/.opencolony/skills/evals/ (with the answers, to look at); the daily sync shares
only the counts (no requests, no answers). A case reaches the skill's owner only as a de-identified copy sent
with a suggestion (suggest.py). Every command prints JSON.
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
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backends  # noqa: E402
import cases  # noqa: E402
import common  # noqa: E402
import office  # noqa: E402

KEEP_BYTES = 30 * 1024 * 1024        # what one run may keep of what it wrote, for looking at afterwards
DEST = {"skill": ".claude/skills/{n}", "command": ".claude/commands/{n}.md", "agent": ".claude/agents/{n}.md"}
JUDGE = ("You grade an answer. Reply with exactly one word, YES or NO.\n\nQuestion about the answer: {q}\n\n"
         "The answer:\n<<<\n{a}\n>>>\n{files}")


def kind_of(skill: str) -> tuple[str, str]:
    kind = "command" if skill.startswith("/") else "agent" if skill.startswith("@") else "skill"
    return kind, skill.lstrip("/@").split(":")[-1]


# ------------------------------------------------------------------ one headless session, on whichever backend is chosen

CANCEL = threading.Event()       # set to stop what is running (the command line's Ctrl-C equivalent)
_local = threading.local()


def set_cancel(event: threading.Event | None) -> None:
    """The app runs each job in its own thread with its own Stop button."""
    _local.cancel = event


def current_cancel() -> threading.Event:
    return getattr(_local, "cancel", None) or CANCEL


def session(prompt: str, workdir: Path, tools: list[str] | None = None, max_turns: int | None = None,
            timeout: int = 900) -> dict:
    """One headless run on the chosen backend (Claude Code or Copilot CLI), in workdir: answer, tokens, seconds,
    the skills and subagents it used (None when the backend does not say), and any error."""
    r = backends.get().headless(prompt, workdir, tools=tools, max_turns=max_turns, timeout=timeout, cancel=current_cancel())
    if not r.get("skills_known", True):
        r["used"] = None
    return r


def make_judge(cache: dict):
    def judge(question: str, answer: str, files: dict[str, str]) -> bool | None:
        shown = "".join(f"\nFile {k}:\n<<<\n{v[:4000]}\n>>>\n" for k, v in list(files.items())[:5] if v)
        prompt = JUDGE.format(q=question, a=answer[:12000], files=shown)
        k = hashlib.sha256(prompt.encode()).hexdigest()
        if k not in cache:
            with tempfile.TemporaryDirectory() as d:
                r = session(prompt, Path(d), max_turns=1, timeout=300)
            words = (r.get("answer") or "").strip().upper().split()
            cache[k] = True if words and words[0].startswith("YES") else False if words and words[0].startswith("NO") else None
        return cache[k]
    return judge


# ------------------------------------------------------------------ arms

def arm_source(arm: str, skill: str, project: Path, proposed: Path | None) -> tuple[Path | None, str]:
    kind, short = kind_of(skill)
    if arm == "none":
        return None, ""
    if arm == "proposed":
        if not proposed:
            raise SystemExit("--proposed is needed for the proposed arm")
        main = proposed / "SKILL.md" if kind == "skill" else proposed
        return proposed, common.digest(main)
    v, _, f = common.find_item(kind, skill.lstrip("/@"), str(project))
    if not f:
        raise SystemExit(f"cannot find {skill} from {project}")
    return (f.parent if kind == "skill" else f), v


def prompt_for(case: dict, arm_has_it: bool) -> str | None:
    via, req = case.get("via", "auto"), case.get("request", "")
    if via.startswith("typed:"):
        if arm_has_it:
            return f"/{via.split(':', 1)[1].split(':')[-1]} {req}".strip()
        return req or None              # nothing to ask without the command itself
    return req or None


def run_one(c: dict, skill: str, project: Path, arm: str, proposed: Path | None = None, i: int = 0,
            judge=None, keep: Path | None = None, source: tuple | None = None) -> dict:
    """One run of one case in one arm, in a fresh folder."""
    kind, short = kind_of(skill)
    src, _ = source or arm_source(arm, skill, project, proposed)
    prompt = prompt_for(c, src is not None)
    row = {"case": c["id"], "arm": arm, "run": i + 1}
    if prompt is None:
        return {**row, "skipped": "the request is the command itself"}
    with tempfile.TemporaryDirectory() as d:
        w = Path(d)
        if (c["dir"] / "files").is_dir():
            shutil.copytree(c["dir"] / "files", w, dirs_exist_ok=True)
        if src is not None:
            dest = w / DEST[kind].format(n=short)
            dest.parent.mkdir(parents=True, exist_ok=True)
            (shutil.copytree if src.is_dir() else shutil.copy2)(src, dest)
        t0 = time.time() - 1
        r = session(prompt, w, tools=c.get("tools"))
        files = cases.written_text(w, since=t0)
        reports = cases.pptx_reports(w, since=t0)
        graded = cases.grade(c.get("checks", []), r["answer"], files, judge=judge or make_judge({}), reports=reports)
        graded += cases.guards(reports)
        images, where = [], ""
        if keep:
            where = f"outputs/{c['id'].replace('/', '_')}/{arm}-{i + 1}"
            out = keep / where
            out.mkdir(parents=True, exist_ok=True)
            (out / "answer.md").write_text(r["answer"], encoding="utf-8")
            total = 0
            for f in cases.written_paths(w, t0):
                total += f.stat().st_size
                if total <= KEEP_BYTES:
                    dest = out / f.relative_to(w)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest)
            for rel in reports:
                if reports[rel]["opens"] and not images:
                    images = [p.relative_to(keep).as_posix() for p in office.render(out / rel, out / "slides")]
    used = (None if r["used"] is None and not (src is not None and prompt.startswith("/")) else
            any(u.lstrip("@").split(":")[-1] == short for u in (r["used"] or [])) or (src is not None and prompt.startswith("/")))
    return {**row, "passed": cases.passed(graded), "checks": [g["ok"] for g in graded],
            "failed_checks": [g.get("judge") or g.get("guard") or json.dumps({k: v for k, v in g.items() if k != "ok"}, ensure_ascii=False)
                              for g in graded if g["ok"] is False],
            "tokens": r["tokens"], "seconds": r["seconds"], "used": used, "error": r["error"],
            "outputs": where, "images": images, "decks": {k: {x: v[x] for x in ("slides", "charts", "slides_with_notes")} for k, v in reports.items()}}


def run(skill: str, project: Path, arms: list[str], proposed: Path | None = None, times: int = 1,
        only: list[str] | None = None, progress=None) -> dict:
    """progress(done, total, row) is called after each run (the app's progress bar)."""
    kind, short = kind_of(skill)
    found = [c for c in cases.find(skill, str(project)) if not only or c["id"] in only]
    if not found:
        raise SystemExit(f"no cases for {skill}: save one with /save-as-case after using it")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    keep = common.home() / "evals" / f"{stamp}-{short}"
    judge = make_judge({})
    sources = {a: arm_source(a, skill, project, proposed) for a in arms}
    plan = [(c, arm, i) for c in found for arm in arms for i in range(times)]
    rows = []
    for n, (c, arm, i) in enumerate(plan):
        if current_cancel().is_set():
            break
        rows.append(run_one(c, skill, project, arm, proposed, i, judge, keep, sources[arm]))
        if progress:
            progress(n + 1, len(plan), rows[-1])
    result = summarise(skill, {a: sources[a][1] for a in arms}, len(found), times, rows)
    keep.mkdir(parents=True, exist_ok=True)
    (keep / "result.json").write_text(json.dumps({**result, "rows": rows}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    compare_page(keep, result, rows, found, common.lang(str(project)))
    return {**result, "kept": str(keep), "page": str(keep / "compare.html")}


PAGE = {"en": {"title": "{skill}: with and without", "arm": {"none": "Without the skill", "current": "Current version", "proposed": "Proposed version"},
               "pass": "passes every check", "fail": "fails", "unknown": "not decided", "tokens": "tokens", "seconds": "s",
               "used": "skill used", "unused": "skill not used", "slides": "slides", "charts": "native charts", "notes": "with notes",
               "request": "Request", "failed": "Failed checks", "none": "(no files)", "files": "Files", "run": "run",
               "no_pic": "No pictures: install LibreOffice and poppler (pdftoppm) to see the slides here."},
        "zh-TW": {"title": "{skill}：有技能與沒技能", "arm": {"none": "沒有技能", "current": "現行版", "proposed": "提議版"},
                  "pass": "全部檢查通過", "fail": "沒通過", "unknown": "無法判定", "tokens": "token", "seconds": "秒",
                  "used": "有用到技能", "unused": "沒用到技能", "slides": "張", "charts": "個原生圖表", "notes": "張有講者備註",
                  "request": "請求", "failed": "沒通過的檢查", "none": "（沒有檔案）", "files": "檔案", "run": "第",
                  "no_pic": "沒有圖片：安裝 LibreOffice 和 poppler（pdftoppm）後，這裡就能直接看到投影片。"}}


def compare_page(keep: Path, result: dict, rows: list[dict], found: list[dict], lang: str = "en") -> None:
    """compare.html: for each case, the arms side by side with what they made (slide pictures when LibreOffice and
    pdftoppm are installed), the checks they failed, tokens and seconds. Everything is escaped."""
    import html
    T = PAGE.get(lang, PAGE["en"])
    e = html.escape
    out = [f"<!doctype html><html lang='{e(lang)}'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>{e(T['title'].format(skill=result['skill']))}</title><style>"
           ":root{--bg:#fbfaf7;--fg:#1d1d1b;--mute:#5d5b56;--line:#dcd9d1;--card:#fff;--good:#17703a;--bad:#b3261e}"
           "@media(prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecece8;--mute:#a7a59e;--line:#3a3a36;--card:#1f1f1d;--good:#5cc489;--bad:#ff8a80}}"
           "body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}main{max-width:1300px;margin:0 auto;padding:20px 16px 60px}"
           ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}"
           ".mute{color:var(--mute)}.good{color:var(--good)}.bad{color:var(--bad)}.thumbs{display:grid;grid-template-columns:repeat(2,1fr);gap:4px;margin:8px 0}"
           ".thumbs img{width:100%;border:1px solid var(--line)}h1{font-size:24px}h2{font-size:18px;margin-top:28px}pre{white-space:pre-wrap;font-size:13px}</style><main>"
           f"<h1>{e(T['title'].format(skill=result['skill']))}</h1><p class=mute>{e(result['verdict'] and ' '.join(say(result, lang).split()) or '')}</p>"]
    for c in found:
        out.append(f"<h2>{e(c['id'])}</h2><p class=mute>{e(T['request'])}: {e((c.get('request') or '')[:400])}</p><div class=grid>")
        for arm in result["arms"]:
            for r in [x for x in rows if x["case"] == c["id"] and x["arm"] == arm]:
                ok = {True: ("good", T["pass"]), False: ("bad", T["fail"]), None: ("mute", T["unknown"])}[r.get("passed")] if "skipped" not in r else ("mute", r["skipped"])
                out.append(f"<div class=card><b>{e(T['arm'].get(arm, arm))}</b> <span class=mute>#{r['run']}</span><br><span class={ok[0]}>{e(ok[1])}</span>"
                           f"<br><span class=mute>{r.get('tokens', 0):,} {e(T['tokens'])} · {r.get('seconds', 0)} {e(T['seconds'])} · "
                           f"{e(T['used'] if r.get('used') else T['unused'])}</span>")
                for path, d in (r.get("decks") or {}).items():
                    out.append(f"<br>{e(path)}: {d['slides']} {e(T['slides'])}, {d['charts']} {e(T['charts'])}, {d['slides_with_notes']} {e(T['notes'])}")
                if r.get("images"):
                    out.append("<div class=thumbs>" + "".join(f"<a href='{e(urllib.parse.quote(i))}'><img loading=lazy src='{e(urllib.parse.quote(i))}' alt=''></a>"
                                                              for i in r["images"][:8]) + "</div>")
                elif r.get("decks"):
                    out.append(f"<p class=mute>{e(T['no_pic'])}</p>")
                if r.get("failed_checks"):
                    out.append(f"<br><span class=mute>{e(T['failed'])}:</span><ul>" + "".join(f"<li>{e(str(x))}</li>" for x in r["failed_checks"]) + "</ul>")
                if r.get("outputs"):
                    out.append(f"<br><a href='{e(urllib.parse.quote(r['outputs']))}/'>{e(T['files'])}</a>")
                out.append("</div>")
        out.append("</div>")
    out.append("</main></html>")
    (keep / "compare.html").write_text("".join(out), encoding="utf-8")


def summarise(skill: str, versions: dict, n_cases: int, times: int, rows: list[dict]) -> dict:
    arms = {}
    for a in versions:
        rs = [r for r in rows if r["arm"] == a and "skipped" not in r]
        arms[a] = {"runs": len(rs), "passed": sum(1 for r in rs if r["passed"] is True),
                   "failed": sum(1 for r in rs if r["passed"] is False), "unknown": sum(1 for r in rs if r["passed"] is None),
                   "errors": sum(1 for r in rs if r.get("error")), "used": sum(1 for r in rs if r.get("used")), "used_known": all(r.get("used") is not None for r in rs),
                   "tokens": round(sum(r["tokens"] for r in rs) / len(rs)) if rs else 0,
                   "seconds": round(sum(r["seconds"] for r in rs) / len(rs), 1) if rs else 0}
    return {"schema": 1, "skill": skill, "user": common.user_handle(), "date": dt.date.today().isoformat(),
            "versions": versions, "cases": n_cases, "times": times, "arms": arms, "verdict": verdict(arms)}


def verdict(arms: dict) -> dict:
    """Fixed rules: results first (share of runs passing every check), then tokens. Compares the current
    version with no skill, and a proposed version with the current one."""
    out = {}
    for new, old in (("current", "none"), ("proposed", "current")):
        a, b = arms.get(new), arms.get(old)
        if not a or not b or not a["runs"] or not b["runs"]:
            continue
        ra, rb = a["passed"] / a["runs"], b["passed"] / b["runs"]
        few = a["runs"] < 5 or b["runs"] < 5
        tok = round(100 * (a["tokens"] - b["tokens"]) / b["tokens"]) if b["tokens"] else 0
        if ra > rb:
            word = "better"
        elif ra < rb:
            word = "worse"
        else:
            word = "same results, fewer tokens" if tok < -10 else "same results, more tokens" if tok > 10 else "no difference"
        out[f"{new}_vs_{old}"] = {"word": word, "passed": [a["passed"], a["runs"]], "passed_before": [b["passed"], b["runs"]],
                                  "tokens_change_pct": tok, "few_runs": few}
    return out


WORDS = {
    "en": {"better": "better", "worse": "worse", "same results, fewer tokens": "same results, fewer tokens",
           "same results, more tokens": "same results, more tokens", "no difference": "no difference",
           "line": "{skill}: {word}. Passes {a} of {n} runs with it, {b} of {m} without; tokens {t:+d}%.",
           "line_p": "{skill}, proposed version against the current one: {word}. Passes {a} of {n}, the current {b} of {m}; tokens {t:+d}%.",
           "few": " Few runs: run more to be sure."},
    "zh-TW": {"better": "比較好", "worse": "比較差", "same results, fewer tokens": "結果一樣、token 較少",
              "same results, more tokens": "結果一樣、token 較多", "no difference": "沒有差別",
              "line": "{skill}：{word}。用它時 {n} 次中通過 {a} 次，不用時 {m} 次中通過 {b} 次；token {t:+d}%。",
              "line_p": "{skill} 提議版對現行版：{word}。提議版 {n} 次中通過 {a} 次，現行版 {m} 次中 {b} 次；token {t:+d}%。",
              "few": "次數少，多跑幾次比較準。"},
}


def say(result: dict, lang: str = "en") -> str:
    W = WORDS.get(lang, WORDS["en"])
    lines = []
    for k, v in result.get("verdict", {}).items():
        tpl = W["line"] if k == "current_vs_none" else W["line_p"]
        word = W[v["word"]]
        lines.append(tpl.format(skill=result["skill"], word=word, a=v["passed"][0], n=v["passed"][1], b=v["passed_before"][0],
                                m=v["passed_before"][1], t=v["tokens_change_pct"]) + (W["few"] if v["few_runs"] else ""))
    return " ".join(lines) or f"{result.get('skill')}: no comparison"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("skill"); r.add_argument("--project", type=Path, default=Path.cwd())
    r.add_argument("--arms", default=None); r.add_argument("--proposed", type=Path); r.add_argument("--times", type=int, default=1)
    r.add_argument("--case", action="append"); r.add_argument("--notify", action="store_true")
    for name in ("checks", "list"):
        p = sub.add_parser(name); p.add_argument("skill"); p.add_argument("--project", type=Path, default=Path.cwd())
    a = ap.parse_args(argv)
    if a.cmd == "run":
        arms = a.arms.split(",") if a.arms else (["none", "current", "proposed"] if a.proposed else ["none", "current"])
        out = run(a.skill, a.project.resolve(), arms, a.proposed.resolve() if a.proposed else None, a.times, a.case)
        out["said"] = say(out, common.lang(str(a.project))) + f" {out['page']}"
        if a.notify:            # shown when the person next starts Claude Code
            with (common.home() / "evals" / "unseen.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"said": out["said"], "kept": out["kept"]}, ensure_ascii=False) + "\n")
    elif a.cmd == "list":
        out = [{"id": c["id"], "skill": c.get("skill"), "request": c.get("request", "")[:200], "checks": c.get("checks"),
                "made_by": c.get("made_by"), "dir": str(c["dir"])} for c in cases.find(a.skill, str(a.project.resolve()))]
    elif a.cmd == "checks":
        judge = make_judge({})
        out = [{"id": c["id"], "problems": cases.weak_checks(c, judge)} for c in cases.find(a.skill, str(a.project.resolve()))]
    common.write_stdout_json(out)


if __name__ == "__main__":
    main()
