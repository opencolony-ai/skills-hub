# skills-hub file formats

The files are the interface. Anything that reads or writes them (the project platform, a script, a person)
gets the same behaviour as the tools here. All JSON is UTF-8; `schema` is 1 everywhere.

## In the hub repository

### `packs/<name>.json` (people write these)

`needs` lists programs the pack's skills cannot work without (`node`, `npm`), `wants` programs that only improve
the result (`soffice`, `pdftoppm`, for pictures of slides). `pull.py doctor` checks them; `make_hub.py --pack
"NAME=skill,skill;needs=node;wants=soffice"` writes one.

What one type of project gets. Names refer to `plugins/<plugin>/skills/<name>/`, `commands/<name>.md` and
`agents/<name>.md` in the hub.

```json
{"title": "Marketing proposal", "description": "...", "skills": ["market-research"], "commands": ["kickoff"], "agents": ["fact-checker"]}
```

`pull.py packs` prints them as `[{name, title, description, items: [{type, name}], missing: ["agent x"]}]`.

### `usage/<person>/<week>.json` (written by each person's hooks; one file per ISO week, rewritten as the week goes on)

```json
{"schema": 1, "client": "claude-code", "user": "ana", "date": "2026-11-13", "week": "2026-W46",
 "days": ["2026-11-09", "2026-11-10", "2026-11-13"],
 "sessions": 4, "sessions_with_skills": 3, "no_skill_sessions": 1, "tokens_no_skill_sessions": 52000,
 "packs": {"proposal": {"sessions": 3}},
 "skills": {
   "market-research": {"type": "skill", "uses": 2, "sessions": 2, "versions": {"1a2b3c4d5e6f": 2},
                       "sources": ["project"], "packs": {"proposal": 2}, "tokens_in_sessions": 310000,
                       "feedback_up": 1, "feedback_down": 0, "notes": [{"rating": "up", "note": "..."}],
                       "corrections_after": 0, "days_used": 2, "last_used": "2026-11-13"},
   "/kickoff": {"type": "command", "...": "..."},
   "@fact-checker": {"type": "agent", "...": "..."}}}
```

Names: a skill as is, a command with `/`, a subagent with `@`. A plugin's copy is named `plugin:name`; the
hub counts `team-skills:x` and `x` as one. A version is the first 12 hex digits of SHA-256 over the main file
(`SKILL.md`, the command's or the subagent's `.md`). Never in this file: prompts, answers, file paths,
repository or project names.

### `data/inventory.json`, `data/stats.json` (recomputed from the above; read them, do not edit)

- inventory: `{schema, made, skills: [{name, type, description, path, repo, version, owner, last_changed,
  commit, by_name_only, same_name, similar}], packs: [as pull.py packs]}`
- stats: `{schema, made, days, since, people, sessions, sessions_with_skills, tokens_per_session_without_skills,
  packs: [{name, sessions, people, uses: {item: n}}], skills: [{name, names, type, uses, sessions, people, days,
  last_used, up, down, corrections_after, notes: [{date, rating, note}], versions, sources, packs,
  tokens_in_sessions, tokens_per_session}]}`

  `evals: {item: latest evaluation}` and `cases: {item: number of shared cases}` too.

`DASHBOARD.md` and `site/index.html` show the same numbers.

`hub.json`: `repos`, `days` (the dashboard's window), `lang` (`en`, `zh-TW`), `feedback` (where suggestions go
when a skill names no `feedback:` of its own: `owner/repo` or a git URL; empty means this hub), `rebuild_hours`
(how stale the dashboard may get before the next person to share rebuilds it; default 1).
`public` (true: anyone can read this hub, so nothing is sent to it without the person seeing it first), `usage`
(`"git"`, or `"none"` for a hub that collects no usage), `checks` (a list of checks, as in a case, asked of every
case of every skill: what the company expects of any answer).

A skill's use in `usage/` may carry `"clients": {"claude-code": 3, "cowork": 2}` and `token_sessions` (the
sessions whose tokens are known; Cowork reports none, so they stay out of the tokens-per-session average).

### `releases.jsonl` (one line per release, appended by `release.py publish`)

```json
{"schema": 1, "skill": "meeting-notes", "version": "79cd3b853446", "previous": "782a8ed41df7", "date": "2026-11-14",
 "by": "ana", "note": "Adds a Risks section and stops asking questions before writing",
 "evidence": {"cases": 3, "times": 1, "which": "small", "word": "no difference", "passed": [3, 3],
              "passed_before": [3, 3], "tokens_change_pct": 0, "few_runs": true},
 "forced": "only when it was released although it did worse, or without cases: the reason"}
```

`release.py publish SKILL --proposed DIR --note TEXT` runs the cases with the current and the proposed version
(`--evidence EVAL_ID` reuses an evaluation of exactly these two versions), refuses when `word` is `worse` unless
`--force REASON`, copies the folder over the skill in the hub, appends the line and pushes with the owner's own
git access. `update.py` (run by the SessionStart hook, at most every ten minutes per project) fetches the hub,
applies the pack again and says what changed, with the note and `passed`; a file changed in the project is left
alone. The inventory carries each skill's last release (`release`) and the latest thirty (`releases`).

### Feedback: a sentence becomes an issue

`/skill-feedback down it asks too many questions` (or the app's box) records the event and, when there are words,
opens an issue `[<skill>] <the sentence>` with labels `skill:<skill>`, `feedback`, `helped` or `did-not-help` in
the skill's feedback repository, with the version and the pack. Never on a public hub. A thumb without words stays
a count.

### Suggestions: GitHub issues in the owner's repository

The skill-suggestion skill shows a draft in the conversation as `~~~~opencolony-suggestion` and
`~~~~opencolony-file case/...` blocks; `/send-suggestion` writes it to `.claude/suggestions/<id>/` and checks it
(`check.json`: `problems` block sending, `look_at` are words in a name's place in the original that are still in
the copy, shown once for the person to confirm). Sending opens an issue titled `[<skill>] <title>` with labels
`skill:<skill>` and the kind, in the repository named by the skill's own front matter (`feedback: owner/repo`),
else `hub.json` `feedback`, else the hub. The token is the person's own: the gh CLI's login, else the HTTPS
login git keeps (Git Credential Manager). The body ends with
`<!-- opencolony {"schema": 1, "skill", "version", "kind", "id", "files"} -->` for programs.

The de-identified copy (case.json, answer.md, files/) goes to the person's OneDrive, in
`OpenColony/cases/<skill>/<id>/`, only if it still fails the case's checks with the current version; the issue
names that folder (or links it, with `"onedrive_url"` in config.json), and the owner sees it once the person
shares it. Nothing of a case goes into a git repository.

### `evals/<person>/<id>.json`

A case, as saved by `/save-as-case` (in a project: `.claude/cases/<item>/<id>/`, with the files the request
named copied into `files/`):

```json
{"schema": 1, "skill": "meeting-notes", "request": "the person's request", "via": "auto",
 "checks": [{"judge": "Every action item has an owner and a due date"}, {"contains": "Owner"}],
 "made_by": "ana", "made_at": "2026-11-14T14:00:24+08:00", "from_version": "782a8ed41df7", "answer_was": "down"}
```

`via` is `auto`, or `typed:<name>` when the person typed `/<name> <request>`. `answer_was` is the 👍/👎 the
person gave in that session, if any; `answer.md` beside it is the answer they got, and `files/` holds files the
request needs. Checks: `contains`, `not_contains`, `regex` (answer and written files), `file` (a glob of written
files), `judge` (a yes/no question a model answers; unknown without one), `pptx` (a presentation the run wrote:
`{"slides_min", "slides_max", "charts_min", "pictures_min", "notes": true, "in_bounds": true, "text": "..."}`; every
key given must hold). Always, without a check: a presentation a run writes must be a sound file (relationships
resolve, XML parses), with no empty slide and no shape outside the slide. `tools` lists the built-in tools the
original session used (`["Write", "Bash"]`); an evaluation allows exactly those.

Small cases: `"small": true, "parent": "<skill>/<id>", "why": "..."`, in `.claude/cases/<skill>/<parent id>-<name>/`.
The case-splitter skill shows them in the conversation and `/save-small-cases` writes them (the app's Split button
runs the same skill without a terminal: `case.split`):

````
~~~~opencolony-case revenue-slide
{"parent": ".claude/cases/pptx/20261114-114019", "request": "...", "why": "...", "checks": [...]}
~~~~
~~~~opencolony-file revenue-slide/files/q.csv
(the file's whole text)
~~~~
~~~~opencolony-copy revenue-slide/files/template.pptx
~~~~
````

A small case must name its original and may not be over 60% of its files' size. `evaluate.py run` and a release
run the small cases when a skill has any (`--which full` the originals, `all` both).

An evaluation, as shared (counts only; the requests and answers stay on the person's machine):

```json
{"schema": 1, "skill": "meeting-notes", "user": "ana", "date": "2026-11-14", "cases": 3, "times": 2,
 "versions": {"none": "", "current": "782a8ed41df7", "proposed": "0c1d2e3f4a5b"},
 "arms": {"none": {"runs": 6, "passed": 1, "failed": 5, "unknown": 0, "errors": 0, "used": 0, "tokens": 41000, "seconds": 18.2},
          "current": {"...": "..."}},
 "verdict": {"current_vs_none": {"word": "better", "passed": [5, 6], "passed_before": [1, 6], "tokens_change_pct": 12, "few_runs": false}}}
```

`word` is one of `better`, `worse`, `same results, fewer tokens`, `same results, more tokens`, `no difference`:
runs passing every check first, then tokens (a change under 10% is no difference).

## In a project (written by `pull.py apply`)

- `.claude/skills/<name>/`, `.claude/commands/<name>.md`, `.claude/agents/<name>.md`: what Claude Code reads.
- `.claude/skills.lock.json`:

  ```json
  {"schema": 1, "hub": "git@github.com:org/skills-hub.git", "hub_commit": "e61ef31...", "pack": "proposal",
   "pulled_at": "2026-11-14T10:00:00+08:00", "tracking": "9f8e7d6c5b4a",
   "items": [{"type": "skill", "name": "market-research", "from": "plugins/team-skills/skills/market-research",
              "to": ".claude/skills/market-research", "version": "1a2b3c4d5e6f", "tree": "0f1e2d3c4b5a"}]}
  ```

  `tree` is one hash over every file of the item; a different hash in the project means someone changed it
  there, and `pull.py apply` leaves it alone. `pull.py check` reports each item as `up to date`,
  `newer in the hub`, `changed here`, `both changed`, `gone from the pack` or `added to the pack`.
- `.claude/opencolony/`: the counting hooks, and `.claude/settings.local.json` entries that run them with the
  full path of the Python that ran `pull.py` (so `python3` need not be on PATH, as on most Windows machines).
  They are marked by the path `.claude/opencolony/record.py`; other settings and hooks are kept, and
  `.claude/.gitignore` keeps the file out of git.
- `.claude/cases/`: the cases saved in this project.
- `proposed/<skill>/`: a copy of a skill being changed (the app's "Start a proposed version"); what
  `release.py publish --proposed` takes. Outside `.claude/`, so it is not a skill Claude Code loads.
- The lock also holds `lang`, `feedback`, `public`, `usage`, `needs`, `wants` from the hub.

`pull.py doctor PROJECT` checks everything counting needs (Python, git, the hub, Claude Code, the hooks, whether
Claude Code trusts the folder, when the hooks last ran, when usage was last shared) and says each problem in
plain words: `{"ok": bool, "checks": [{"name", "ok", "say"}]}`.

## On each person's machine (`~/.opencolony/skills/`, or `OPENCOLONY_SKILLS_HOME`)

- `events/<date>.jsonl`: one event per line, `{"at": ISO time, "kind": ...}`:
  - `use`: `session, skill, type, version, source, pack, project` (`project` is a hash of the folder, local only)
  - `feedback`: `session, skill, rating ("up"|"down"), note`; the project platform's 👍/👎 records it with
    `feedback.py give --skill NAME --rating up|down [--note TEXT] --project DIR`, and asks what to show with
    `feedback.py recent --project DIR` (`{"items": [{name, type, last_used, rated, version}]}`)
  - `correction`: `session, skill`
  - `session`: `session, tokens: {input, output, cache_write, cache_read, total, models, skills, turns}`
  - `update`: `project, changed: [items], hub_commit` (a session start brought new versions)
  - a `use` or `feedback` from Cowork has `client: "cowork"` and the time Cowork wrote in the file's name
- `cowork.json`: `{pack, hub, items: [{name, version, tree}], installed_at, collected_at, collected: {uses, feedback}}`.
- `config.json` (optional): `{"user": "name to share under", "hub": "git URL", "feedback": "owner/repo",
  "onedrive": "folder", "onedrive_url": "https://acme-my.sharepoint.com/personal/ana_acme_com/Documents", "nudge": true}`.
- `notices.jsonl`: results of background work (a suggestion sent or not) not shown yet.
- `synced.json`, `synced_evals.json`: what was already shared. `hub/`: the clone used to share it.
- `evals/<id>/result.json`, `compare.html` and `outputs/<case>/<arm>-<n>/` (the answer, every file the run wrote
  up to 30 MB, and `slides/*.png` when LibreOffice and pdftoppm exist): each evaluation in full, to look at; `evals/unseen.jsonl`: results not yet shown
  (the next session start shows them). `cases/`: cases saved outside a project. `heartbeat`: when a hook last ran.

## In the person's OneDrive (Microsoft 365 Copilot Cowork)

- `Documents/Cowork/skills/<name>/SKILL.md` (+ the skill's files): written by `cowork.py install PACK`, refreshed by
  `cowork.py sync`. `name:` equals the folder; the text ends with `<!-- opencolony:report-back -->` and a section
  asking Cowork to save, once per task, `oc-<name>-<YYYYMMDD-HHMMSS>.txt` in its output folder:

  ```
  skill: meeting-notes
  version: 79cd3b853446
  result: done
  rating: down
  feedback: it asked me too many questions
  ```

  `cowork.py sync` reads those files (in `<OneDrive>/Cowork/`, `<OneDrive>/Documents/Cowork/` or `"cowork_output"`
  in config.json), records a `use` (and a `feedback` when there is a rating or words; `failed` counts as 👎), sends
  the words to the owner like any other sentence, and moves the file to `opencolony/processed/`. Skills that do not
  fit Cowork's limits (20 files, 10 MB, 5 MB each, 50 skills, kebab-case name) are reported and not installed;
  a skill the person changed in OneDrive, or one this program did not put there, is never touched.
