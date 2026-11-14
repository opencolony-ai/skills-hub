---
name: skill-suggestion
description: Turn what the person says about one of the company's skills, commands or subagents (something it got wrong, something it should do, or what worked well) into a suggestion for the skill's owner. Use when the person complains about or praises a skill they just used, says it should work differently, or asks to send feedback or a suggestion to whoever maintains it.
---

# Suggestion for a skill's owner

You write the suggestion in the conversation; a program checks it and sends it, only after the person types
`/send-suggestion`. Write no files for it, and never send anything yourself: no git, no gh, no web requests.
Never edit any SKILL.md, command or subagent file: owners change their skills, from suggestions like this one.

## 1. Which skill, and what the person means

- The skill is the one the person is talking about, usually the last one used in this session. If you are not
  sure, ask which one (one short question).
- Ask at most one more question, only if you cannot tell what a better result would look like.

## 2. Draft it, in the person's language

```
Title: one line, what should change

What happened: one to three sentences.
What would be better: one to three sentences, as concrete as possible.
Example: (optional) a short made-up example that shows it.
```

Keep out anything confidential: people's names, clients, amounts, internal project names. Use placeholders
such as "Client A", "Person B", "NT$X".

## 3. If the owner needs the files to see the problem

A case is the request, the answer and the files, saved on this computer with `/save-as-case`. The owner only
ever gets a made-up copy of it, never the original.

- If `.claude/cases/<skill>/` has no case for this request, ask the person to type
  `/save-as-case` followed by what a good answer must have, then continue.
- Read the case (`case.json`, `answer.md`, `files/`) and make the copy:
  - `case/case.json`: the original with `request` rewritten using made-up names, numbers and dates; keep
    `checks` unless a check names real people or clients.
  - `case/answer.md`: the original answer with the same made-up values.
  - `case/files/<name>`: every file the request needs, rewritten with made-up values. Keep what made the problem
    happen: the structure, the columns, the layout, empty cells, odd formats, the edge case. For a Word, Excel or
    PowerPoint file, write a `.md` or `.csv` file with the same structure. Leave out PDF files and images;
    describe them in words in the suggestion instead.
- Replace every name, e-mail address, phone number, amount, account or order number, and address. A program
  compares the copy with the original and refuses to send it if any of these survived.

## 4. Show exactly what will leave, as blocks

Show the suggestion and every file of the copy in these blocks (four tildes; the program reads them from this
conversation, so keep the format exactly):

~~~~~
~~~~opencolony-suggestion
skill: meeting-notes
kind: problem
title: Action items need a due date even when only a weekday was said
from_case: .claude/cases/meeting-notes/20261114-150012
---
What happened: ...
What would be better: ...
Example: ...
~~~~

~~~~opencolony-file case/case.json
{ ...the made-up copy... }
~~~~

~~~~opencolony-file case/files/meeting.txt
...the made-up copy...
~~~~
~~~~~

`kind` is `problem`, `idea` or `praise`. Leave out `from_case` and the file blocks when there is no copy.

Then say where it goes: an issue in the skill owner's GitHub repository, which everyone who can open that
repository can read; the files go to the person's own OneDrive, and the owner sees them only if the person shares
that folder. Tell the person to type `/send-suggestion` to send it, or to tell you what to change (then show the
blocks again, changed). Do nothing else.
