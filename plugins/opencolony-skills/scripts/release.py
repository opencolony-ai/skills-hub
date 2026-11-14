#!/usr/bin/env python3
"""Release a new version of a skill to everyone, with evidence: the owner's proposed version is first run against
the skill's cases, beside the current version, and it is published to the hub only if it does no worse.

    release.py publish SKILL --proposed DIR --note "what changed and why" [--project DIR] [--times 1]
                             [--evidence EVAL_ID] [--force "why it is released anyway"]
    release.py list [SKILL]                      what was released, newest first

Publishing copies the proposed folder over the skill in the hub, appends one line to the hub's releases.jsonl
(who, when, the note, the cases' result) and pushes with the owner's own git access. Everyone's projects pick it
up the next time they start a session (update.py), with the note.

Programs decide: the rule is fixed (runs passing every check must not go down), a model only answers the yes/no
questions in the cases, and a person wrote the note and chose to release. --force is recorded with its reason.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cases  # noqa: E402
import common  # noqa: E402
import evaluate  # noqa: E402
import sync  # noqa: E402
import update  # noqa: E402

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", ".git")


def evidence_from(result: dict) -> dict:
    v = (result.get("verdict") or {}).get("proposed_vs_current") or {}
    return {"cases": result.get("cases", 0), "times": result.get("times", 1), "which": result.get("which", ""), "word": v.get("word", ""),
            "passed": v.get("passed"), "passed_before": v.get("passed_before"), "tokens_change_pct": v.get("tokens_change_pct"),
            "few_runs": v.get("few_runs")}


def saved_evaluation(eval_id: str, skill: str, proposed_version: str) -> dict:
    """An evaluation already run (in the app, or by evaluate.py), if it is of this skill and this very version."""
    f = common.home() / "evals" / eval_id / "result.json"
    try:
        r = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SystemExit(f"no evaluation {eval_id}")
    if evaluate.kind_of(r.get("skill", ""))[1] != evaluate.kind_of(skill)[1]:
        raise SystemExit(f"evaluation {eval_id} is of {r.get('skill')}, not {skill}")
    if (r.get("versions") or {}).get("proposed") != proposed_version:
        raise SystemExit("that evaluation ran a different proposed version: the folder changed since, so run it again")
    return r


def publish(skill: str, proposed: Path, note: str, project: Path, times: int = 1, evidence: str = "",
            force: str = "", progress=None, which: str = "auto") -> dict:
    kind, short = evaluate.kind_of(skill)
    if kind != "skill":
        raise SystemExit("only skills are released this way for now")
    if not (proposed / "SKILL.md").is_file():
        raise SystemExit(f"{proposed} has no SKILL.md")
    if not note.strip():
        raise SystemExit("say what changed and why (--note): everyone who gets the new version reads it")
    version = common.digest(proposed / "SKILL.md")
    current = common.find_item("skill", short, str(project))[0]
    if version == current:
        raise SystemExit("the proposed version is the same as the current one")
    if evidence:
        result = saved_evaluation(evidence, skill, version)
    elif not cases.find(skill, str(project)):
        if not force:
            raise SystemExit(f"no cases for {skill}: save one with /save-as-case (a small one is enough), or release without evidence with --force and the reason")
        result = {}
    else:
        result = evaluate.run(skill, project, ["current", "proposed"], proposed, times, progress=progress, which=which)
    ev = evidence_from(result) if result else {"cases": 0, "word": "not evaluated"}
    if ev.get("word") == "worse" and not force:
        raise SystemExit(f"not released: the proposed version passes {ev['passed'][0]} of {ev['passed'][1]} runs, the current one "
                         f"{ev['passed_before'][0]} of {ev['passed_before'][1]}. Look at {result.get('page') or result.get('kept', '')}, "
                         "or release anyway with --force and the reason")
    entry = {"schema": 1, "skill": short, "version": version, "previous": current, "date": dt.date.today().isoformat(),
             "by": common.user_handle(), "note": " ".join(note.split())[:500], "evidence": ev,
             **({"forced": " ".join(force.split())[:300]} if force else {})}

    def place(clone: Path) -> list[str]:
        found = sorted(p for p in clone.glob(f"plugins/*/skills/{short}") if p.parts[-3] != "opencolony-skills")
        if not found:
            raise SystemExit(f"the hub has no skill {short}")
        target = found[0]
        keep = {n: (target / n).read_bytes() for n in ("PROVENANCE.json",) if (target / n).is_file()}
        shutil.rmtree(target)
        shutil.copytree(proposed, target, ignore=IGNORE)
        for n, data in keep.items():                    # where it first came from stays on record, marked as changed
            try:
                j = json.loads(data.decode("utf-8"))
                j["changed"] = True
                (target / n).write_text(json.dumps(j, indent=2) + "\n", encoding="utf-8")
            except ValueError:
                (target / n).write_bytes(data)
        with (clone / "releases.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return [target.relative_to(clone).as_posix(), "releases.jsonl"]
    if not sync.hub_url(str(project)):
        raise SystemExit("no hub is known for this project")
    if not sync.publish(place, f"release: {short} {version} ({entry['by']})", cwd=str(project), rebuild_now=True, even_if_public=True):
        raise SystemExit("could not push to the hub: check that you can write to it (owners need write access)")
    return {"released": True, **entry, "page": result.get("page", "") if result else ""}


def listing(project: Path, skill: str = "") -> list[dict]:
    clone = update.refresh(sync.hub_url(str(project)) or "") if sync.hub_url(str(project)) else None
    out = update.releases(clone or common.home() / "hub")
    short = evaluate.kind_of(skill)[1] if skill else ""
    return [r for r in reversed(out) if not short or r.get("skill") == short]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish")
    p.add_argument("skill"); p.add_argument("--proposed", type=Path, required=True); p.add_argument("--note", required=True)
    p.add_argument("--project", type=Path, default=Path.cwd()); p.add_argument("--times", type=int, default=1)
    p.add_argument("--evidence", default=""); p.add_argument("--force", default="")
    p.add_argument("--full", action="store_true", help="verify on the original cases, not the small ones")
    l = sub.add_parser("list"); l.add_argument("skill", nargs="?", default=""); l.add_argument("--project", type=Path, default=Path.cwd())
    a = ap.parse_args(argv)
    if a.cmd == "publish":
        out = publish(a.skill, a.proposed.expanduser().resolve(), a.note, a.project.resolve(), a.times, a.evidence, a.force,
                      which="full" if a.full else "auto")
    else:
        out = listing(a.project.resolve(), a.skill)
    common.write_stdout_json(out)


if __name__ == "__main__":
    main()
