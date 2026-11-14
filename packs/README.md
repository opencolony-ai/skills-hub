One file per type of project: `packs/<name>.json` lists what a project of that type gets.

```json
{
  "title": "Marketing proposal",
  "description": "Research, outline and deck for a client proposal.",
  "skills": ["market-research", "proposal-outline"],
  "commands": ["kickoff"],
  "agents": ["fact-checker"]
}
```

Names refer to `plugins/<plugin>/skills/<name>/`, `plugins/<plugin>/commands/<name>.md` and
`plugins/<plugin>/agents/<name>.md` in this repository. `python3 hub/pull.py packs` lists the packs and
anything they name that cannot be found; `python3 hub/pull.py apply <pack> <project folder>` brings one into a
project (that is what the project platform runs).
