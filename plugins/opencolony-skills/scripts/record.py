#!/usr/bin/env python3
"""The hook: Claude Code runs `record.py <event>` with the event as JSON on stdin. It runs from the
opencolony-skills plugin, or from a project's .claude/opencolony/ when a pack was pulled into the project
(pull.py); when both are installed each thing is still counted once.

  SessionStart       bring the project's skills up to date from the hub and say what changed (update.py); start
                     the daily sync in the background; tell the person about work that finished since last time
  UserPromptSubmit   the plugin's own commands are handled here and stopped before they reach Claude (no tokens,
                     no tool permission needed):
                       /skill-feedback up|down [skill] [note]
                       /save-as-case [what a good answer must have]   the last request becomes a case
                       /save-small-cases                              save the small cases the case-splitter skill showed
                       /skill-eval [skill] [--times N]                run the cases in the background (small ones first)
                       /send-suggestion                               send what the skill-suggestion skill
                                                                      wrote, once a program has checked it
                     A typed /command or /skill that exists as a file is a use. If the person corrects Claude
                     right after a skill ("no, that's wrong"), a correction is recorded against it. The prompt
                     itself is never kept.
  PostToolUse        Skill: a use of that skill. Agent (or Task): a use of that subagent, when it is one of the
                     company's (a file in agents/), not a built-in one.
  Stop               a suggestion just written: check it and say what would leave; results of background work;
                     else, once per session and skill, a one-line reminder that feedback and cases exist
  SessionEnd         the session's tokens and the skills it used, from its transcript

It must never get in the way: any error is swallowed and the hook exits 0.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import feedback  # noqa: E402

OWN = "opencolony-skills:"          # this plugin's own commands are not skills to count
PREFIX = r"^\s*/(?:opencolony-skills:)?"
FEEDBACK = re.compile(PREFIX + r"skill-feedback\b(.*)$", re.S)
SAVE = re.compile(PREFIX + r"save-as-case\b(.*)$", re.S)
SMALL = re.compile(PREFIX + r"save-small-cases\b(.*)$", re.S)
EVAL = re.compile(PREFIX + r"skill-eval\b(.*)$", re.S)
SEND = re.compile(PREFIX + r"send-suggestion\b(.*)$", re.S)
OWN_SKILLS = common.OWN_SKILLS
SLASH = re.compile(r"^\s*/([A-Za-z0-9][\w.-]*(?::[\w.-]+)*)(?:\s|$)")

SAY = {
    "en": {"nudge": "Used {x}. Did it help? /skill-feedback up, or /skill-feedback down and why. "
                    "To keep this request as a test of {x}: /save-as-case and what a good answer must have.",
           "saved": "Saved a case for {x} ({d}).{c} Run the cases: /skill-eval {x}",
           "check": " Its check: “{n}”.", "nocheck": " Add what a good answer must have to its case.json, or save again "
                    "with /save-as-case and a sentence.",
           "nothing": "Nothing to save yet: use a skill first, then /save-as-case.",
           "eval": "Running {n} case(s) of {x} in the background (uses your Claude quota). The result shows the next time "
                   "you start Claude Code, and on the hub's dashboard.",
           "nocases": "No cases for {x} yet: after using it, /save-as-case and what a good answer must have.",
           "noskill": "Which skill? /skill-eval <skill>",
           "small": "Saved {n} small case(s) ({a} bytes of files, the original has {b}). /skill-eval now runs these.",
           "nosmall": "No small cases to save. Ask Claude to split a case into small ones first.",
           "ready": "Suggestion for {x} is ready: an issue in {repo}{files}. Send it with /send-suggestion.",
           "files": ", with {n} file(s) going to your OneDrive",
           "look": " Check these words are made up, not real: {w}.",
           "blocked": "Suggestion for {x} cannot be sent yet: {p}",
           "sending": "Sending the suggestion in the background. You will see the result here when it is done.",
           "nosugg": "No suggestion to send. Say what should change about a skill, and Claude will write one.",
           "again": "If they are, type /send-suggestion again to send it; if not, ask Claude to replace them."},
    "zh-TW": {"nudge": "這次用了 {x}。有幫上忙嗎？/skill-feedback up，或 /skill-feedback down 加原因。"
                       "想把這題留下來測 {x}：/save-as-case 加上「好答案應該…」。",
              "saved": "已存成 {x} 的考題（{d}）。{c}跑考題：/skill-eval {x}",
              "check": "檢查項目：「{n}」。", "nocheck": "請在 case.json 寫上好答案應該有什麼，或重新 /save-as-case 加一句話。",
              "nothing": "還沒有可以存的內容：先用一個技能，再 /save-as-case。",
              "eval": "在背景跑 {x} 的 {n} 題考題（用你的 Claude 額度）。結果會在下次開 Claude Code 時顯示，也會出現在 hub 儀表板。",
              "nocases": "{x} 還沒有考題：用過之後，/save-as-case 加上「好答案應該…」。",
              "noskill": "要評估哪個技能？/skill-eval <技能>",
              "small": "已存 {n} 題小考題（檔案共 {a} bytes，原題 {b}）。之後 /skill-eval 會跑這些小題。",
              "nosmall": "沒有可以存的小考題。先請 Claude 把一題考題拆成小題。",
              "ready": "給 {x} 的建議準備好了：會在 {repo} 開一個 issue{files}。要送出請輸入 /send-suggestion。",
              "files": "，{n} 個檔案存到你的 OneDrive",
              "look": "請確認這些字是假的、不是真實資料：{w}。",
              "blocked": "給 {x} 的建議還不能送出：{p}",
              "sending": "在背景送出建議，完成後會在這裡顯示結果。",
              "nosugg": "沒有要送出的建議。說說技能哪裡該改，Claude 會幫你寫成建議。",
              "again": "如果都是假的，再輸入一次 /send-suggestion 就會送出；如果不是，請 Claude 換掉。"},
}


def say(key: str, data: dict, **kw) -> str:
    return SAY.get(common.lang(data.get("cwd", "")), SAY["en"])[key].format(**kw)


def use(kind: str, name: str, data: dict) -> None:
    if name.split(":")[-1] in OWN_SKILLS:
        return
    sid = data.get("session_id", "")
    version, source, _ = common.find_item(kind, name, data.get("cwd", ""))
    if kind != "skill" and not version:
        return                      # a built-in command or subagent, not one of the company's
    k = common.key(kind, name)
    lock = common.project_lock(data.get("cwd", ""))
    common.append({"kind": "use", "session": sid, "skill": k, "type": kind, "version": version, "source": source,
                   "pack": lock.get("pack", ""), "project": common.project_id(data.get("cwd", ""))})
    st = common.state(sid)
    st["last_skill"] = k
    st.setdefault("skills", [])
    if k not in st["skills"]:
        st["skills"].append(k)
    common.save_state(sid, st)


def background(script: str, *args: str) -> None:
    subprocess.Popen([sys.executable, str(Path(__file__).with_name(script)), *args], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ.copy(), **common.detach_kwargs())


def finished_evaluations() -> str:
    """Results of background work (evaluations, suggestions sent) not shown yet."""
    lines = []
    for f in (common.home() / "evals" / "unseen.jsonl", common.home() / "notices.jsonl"):
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                lines.append(json.loads(line)["said"])
            except (ValueError, KeyError):
                pass
        f.unlink()
    return " ".join(lines)


def suggestions_ready(data: dict) -> str:
    """A draft Claude just showed: check it now and say what would leave, before the person sends it."""
    import suggest
    d = suggest.materialize(data.get("transcript_path", ""), data.get("cwd", ""), last_turn_only=True)
    if not d or (d / "sent.json").exists() or (d / "announced.json").exists():
        return ""
    (d / "announced.json").write_text("{}", encoding="utf-8")
    s, chk = suggest.load(d), suggest.check(d, data.get("cwd", ""))
    if chk["problems"]:
        return say("blocked", data, x=s["skill"], p="; ".join(chk["problems"]))
    host, owner, repo = suggest.destination(s["skill"], data.get("cwd", ""))
    msg = say("ready", data, x=s["skill"], repo=f"{owner}/{repo}" if owner else "?",
              files=say("files", data, n=len(chk["files"])) if chk["files"] else "")
    if chk["look_at"]:
        msg += say("look", data, w="、".join(chk["look_at"][:10]))
    return msg


def own_command(prompt: str, data: dict) -> str | None:
    """The reply to one of the plugin's own commands, or None when the prompt is not one."""
    sid, cwd = data.get("session_id", ""), data.get("cwd", "")
    m = FEEDBACK.match(prompt)
    if m:
        return feedback.main(m.group(1).split(), cwd)
    if SMALL.match(prompt):
        import cases
        r = cases.save_small(data.get("transcript_path", ""), cwd)
        if not r["saved"] and not r["problems"]:
            return say("nosmall", data)
        msg = say("small", data, n=len(r["saved"]), a=f"{r['bytes']:,}", b=f"{r['parent_bytes']:,}") if r["saved"] else ""
        return (msg + " " + " ".join(r["problems"])).strip()
    m = SAVE.match(prompt)
    if m:
        import cases
        st = common.state(sid)
        skill = st.get("last_skill", "")
        d, why = cases.save(data.get("transcript_path", ""), skill, m.group(1).strip(), cwd,
                            rating=(st.get("ratings") or {}).get(skill, ""))
        if not d:
            return say("nothing", data)
        c = json.loads((d / "case.json").read_text(encoding="utf-8"))
        note = m.group(1).strip()
        return say("saved", data, x=c["skill"] or "?", d=d, c=say("check", data, n=note) if note else say("nocheck", data))
    if SEND.match(prompt):
        import suggest
        d = suggest.materialize(data.get("transcript_path", ""), cwd)
        todo = [d] if d and not (d / "sent.json").exists() else suggest.pending(cwd)
        if not todo:
            return say("nosugg", data)
        replies = []
        for d in todo:
            s, chk = suggest.load(d), suggest.check(d, cwd)
            if chk["problems"]:
                replies.append(say("blocked", data, x=s["skill"], p="; ".join(chk["problems"])))
            elif chk["look_at"] and not (d / "shown.json").exists():
                (d / "shown.json").write_text("{}", encoding="utf-8")       # asked once; the next /send-suggestion sends
                replies.append(say("look", data, w="、".join(chk["look_at"][:10])).strip() + " " + say("again", data))
            else:
                background("suggest.py", "send", str(d), "--notify")
                replies.append(say("sending", data, n=1))
        return " ".join(replies)
    m = EVAL.match(prompt)
    if m:
        import cases
        words = m.group(1).split()
        times = "1"
        if "--times" in words and words.index("--times") + 1 < len(words):
            times = words[words.index("--times") + 1]
            del words[words.index("--times"):words.index("--times") + 2]
        skill = words[0] if words else common.state(sid).get("last_skill", "")
        if not skill:
            return say("noskill", data)
        found = cases.find(skill, cwd)
        if not found:
            return say("nocases", data, x=skill)
        background("evaluate.py", "run", skill, "--project", str(common.project_root(cwd)), "--times", times, "--notify")
        return say("eval", data, x=skill, n=len(found))
    return None


def main(event: str, data: dict) -> None:
    sid = data.get("session_id", "")
    common.home().mkdir(parents=True, exist_ok=True)
    (common.home() / "heartbeat").write_text(str(int(time.time())), encoding="utf-8")   # pull.py doctor reads it
    if event == "SessionStart":
        if not common.first_time(sid, event, data.get("source", "")):
            return
        common.forget_old_events()
        said = []
        try:                                   # bring the project's skills up to date before the session uses them
            import update
            said.append(update.run(data.get("cwd", "")).get("said", ""))
        except BaseException:                  # noqa: BLE001 - an update that cannot be done is not this session's problem
            pass
        background("sync.py", "--cwd", data.get("cwd", ""))
        said.append(finished_evaluations())
        said = " ".join(x for x in said if x)
        if said:
            common.write_stdout_json({"systemMessage": said})
        return
    if event == "UserPromptSubmit":
        prompt = data.get("prompt", "")
        once = common.first_time(sid, event, hashlib.sha256(prompt.encode()).hexdigest(), window=2)
        if FEEDBACK.match(prompt) or SAVE.match(prompt) or SMALL.match(prompt) or EVAL.match(prompt) or SEND.match(prompt):
            if once:    # the other copy of this hook (if any) has blocked it already
                (common.home() / "last_session").write_text(sid, encoding="utf-8")
                common.write_stdout_json({"decision": "block", "reason": own_command(prompt, data)})
            return
        if not once:
            return
        (common.home() / "last_session").write_text(sid, encoding="utf-8")
        st = common.state(sid)
        if st.get("last_skill") and common.is_correction(prompt):
            common.append({"kind": "correction", "session": sid, "skill": st["last_skill"]})
        s = SLASH.match(prompt)
        if s and not s.group(1).startswith(OWN):
            name = s.group(1)
            for kind in ("skill", "command"):
                if common.find_item(kind, name, data.get("cwd", ""))[0]:
                    use(kind, name, data)
                    st = common.state(sid)
                    st["typed"] = common.key(kind, name)      # Claude may also call the Skill tool for it
                    common.save_state(sid, st)
                    break
        return
    if event == "PostToolUse":
        tool, inp = data.get("tool_name"), data.get("tool_input") or {}
        if not common.first_time(sid, event, data.get("tool_use_id") or json.dumps(inp, sort_keys=True)):
            return
        if tool == "Skill":
            name = inp.get("skill") or ""
            if not name or name.startswith(OWN):
                return
            st = common.state(sid)
            if st.get("typed") == name:             # already counted when the person typed /name
                st.pop("typed")
                common.save_state(sid, st)
                return
            use("skill", name, data)
        elif tool in ("Agent", "Task") and inp.get("subagent_type"):
            use("agent", inp["subagent_type"], data)
        return
    if event == "Stop":
        shown = " ".join(x for x in (finished_evaluations(), suggestions_ready(data)) if x)
        if shown:
            common.write_stdout_json({"systemMessage": shown})
            return
        st = common.state(sid)
        todo = [k for k in st.get("skills", []) if k not in st.get("nudged", []) and k not in st.get("rated", [])]
        if todo and common.config().get("nudge", True) and common.first_time(sid, event, todo[-1]):
            st["nudged"] = st.get("nudged", []) + todo
            common.save_state(sid, st)
            common.write_stdout_json({"systemMessage": say("nudge", data, x=", ".join(todo))})
        return
    if event == "SessionEnd":
        t = data.get("transcript_path", "")
        if common.first_time(sid, event, str(os.path.getsize(t)) if t and os.path.isfile(t) else ""):    # a resumed session ends again, longer
            common.append({"kind": "session", "session": sid, "tokens": common.transcript_summary(t)})


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "", common.read_stdin_json())
    except Exception:  # never break the person's session
        pass
    sys.exit(0)
