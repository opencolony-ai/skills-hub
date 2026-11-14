---
name: case-splitter
description: Cut a big saved test case of a skill into a few small ones that each show one thing, so the skill's owner can try changes without re-running the whole big task. Use when the person asks to split, shrink or break down a case, to make small cases or a smaller test, or says that testing or iterating on a skill costs too many tokens.
---

# Cut a big case into small ones

A case is a saved request for a skill, with its files and the checks a good answer must pass
(`.claude/cases/<skill>/<id>/`: `case.json`, `answer.md`, `files/`, sometimes `produced/`). Running a big one over
and over while a skill is being changed is slow and costly. You cut it into small cases; a program saves them when
the person types `/save-small-cases`. Write no files yourself.

## 1. Read the big case

Ask which case if it is not clear (list `.claude/cases/<skill>/`). Read its `case.json`, its `files/`, and
`answer.md` (and `produced/` if there is one) to see what was asked, what came out, and which checks it must pass.

## 2. Find the separate things it tests

List what a good answer depends on, one per line: a step of the work (reading the data, the analysis, one kind of
slide or section, the wording of a conclusion), or one place the skill is known to go wrong. Each becomes one
small case. Three to six is usual. Do not make a small case for something the big one does not test.

## 3. Make each small case small

- **Request**: the same kind of request, narrowed to that one thing ("only the revenue-trend slide", "only the
  customer table for March").
- **Files**: the least that still shows it. Keep the real structure (columns, headings, odd values, the edge case),
  drop the rest: a few rows, not the whole table; two or three pages, not a hundred. Small text files you write out
  in full; a file needed unchanged (a template) is copied from the original.
- **Checks**: only the original's checks that this piece can pass or fail, reworded for the piece if needed. Add a
  check that can be decided by a program where you can (`contains`, `regex`, `file`,
  `{"pptx": {"slides_max": 3, "charts_min": 1}}`); keep `judge` for what needs reading.
- Aim for each small case to run in a small fraction of the original's time. If a piece cannot be made much
  smaller than the original, say so and leave it out.

## 4. Show them as blocks

One `opencolony-case` block per small case (its name: short, lower case, hyphens), then its files. Four tildes;
keep the format exactly, a program reads it.

~~~~~
~~~~opencolony-case revenue-slide
{"parent": ".claude/cases/pptx/20261114-114019",
 "request": "Using q.csv, make only the revenue trend slide.",
 "why": "the trend chart is where labels went wrong",
 "checks": [{"pptx": {"slides_max": 2, "charts_min": 1}}, {"judge": "The chart shows revenue by quarter with data labels"}]}
~~~~

~~~~opencolony-file revenue-slide/files/q.csv
quarter,revenue
2026Q1,162
2026Q2,185
2026Q3,203
~~~~

~~~~opencolony-copy revenue-slide/files/template.pptx
~~~~
~~~~~

`opencolony-file NAME/files/PATH` holds a file's whole text. `opencolony-copy NAME/files/PATH` copies that file
unchanged from the original case's `files/` (leave the block empty).

## 5. Say what to do next

In two lines: how many small cases, what each one tests, and that `/save-small-cases` saves them; after that
`/skill-eval <skill>` runs the small ones (the original is kept for a last check before a release). Do nothing else.
