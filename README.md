# OpenColony skills hub (public)

A shared list of skills for Claude Code and GitHub Copilot, grouped by the kind of project they are used in, and a place
to say what worked and what did not. Companies can copy it as their own internal hub.

繁體中文：這是一個公開的技能庫。用 [OpenColony Skills](https://github.com/opencolony-ai/OpenColony) 連接它，依專案類型把技能拉進專案，
用你自己的考題評估，並把意見回報到這個 repo 的 Issues。公司可以複製一份成為內部技能庫（見下方「複製成公司內部的 hub」）。

## What is in it

| Skill | From | License | Needs |
|---|---|---|---|
| `pptx` | [anthropics/skills](https://github.com/anthropics/skills) | Apache-2.0 | Node.js, npm (LibreOffice to check slides) |
| `docx` | anthropics/skills | Apache-2.0 | Node.js, npm (LibreOffice, pandoc) |
| `xlsx` | anthropics/skills | Apache-2.0 | Python (LibreOffice to recalculate) |
| `pdf` | anthropics/skills | Apache-2.0 | Python |
| `internal-comms` | anthropics/skills | Apache-2.0 | |

Each is a copy of one pinned commit (`plugins/anthropic-skills/skills/<name>/PROVENANCE.json` says which), with its own
`LICENSE.txt`. Nothing here is changed from upstream. `sources.json` is the list; `make_public_hub.py` in OpenColony builds
this repository from it.

## Project types (packs)

`packs/*.json`: what a project of that type gets. `client-proposal`, `reporting`, `documents`, `all`.

## Use it

In [OpenColony Skills](https://github.com/opencolony-ai/OpenColony/tree/main/skills-hub): *Start → connect to the hub →*
`opencolony-ai/skills-hub`, then *Projects → add a project* and choose a type. Everything the app does with it (pulling,
evaluating with your own cases, feedback) is described in its quick start.

## Say what you think

Feedback and suggestions about a skill are GitHub issues in this repository (`skill:<name>` label). **Issues here are
public.** The app shows you exactly what would be sent and asks first; files from your own cases are never put in an
issue (they are made up first, and go to your own OneDrive). Anyone with a GitHub account can open one.

Usage numbers are not collected by this hub yet (`hub.json` says `"usage": "none"`): nothing is pushed here.

## Copy it as your company's own hub

A public repository cannot be forked into a private one, so copy it:

```bash
git clone https://github.com/opencolony-ai/skills-hub.git acme-skills
cd acme-skills
git remote rename origin upstream
git remote add origin git@github.com:acme/acme-skills.git     # an empty private repository of yours
git push -u origin main
```

(If your company is fine with a public fork, the *Fork* button does the same and keeps the link.) Then in your copy:

1. `hub.json`: set `"public": false`, `"feedback"` to the repository where your people's suggestions should become issues
   (their own team's, or this one), and `"usage"` to `"git"` if everyone who should be counted can push to it.
2. Put your own skills in `plugins/<your-company>-skills/skills/<name>/SKILL.md` (add `owner:` to the front matter), list
   that plugin in `.claude-plugin/marketplace.json`, and name them in `packs/*.json`.
3. People connect OpenColony Skills to your repository instead of this one.
4. To take new skills and fixes from here later: `git pull upstream main` (conflicts, if any, are in `hub.json`,
   `marketplace.json` and `packs/`).

## Privacy

This repository holds no usage data and no one's files. What an issue contains is what its author reviewed and sent.

## License

The hub's own files (`packs/`, `hub/`, `plugins/opencolony-skills/`, this README): Apache-2.0. Each skill keeps the license in its
own folder; see `plugins/anthropic-skills/THIRD_PARTY_NOTICES.md` for what those skills bundle.
