---
name: writing
description: Jeremy's longform writing process, preferences, and human-in-the-loop Google Doc collaboration protocol. Use when helping draft, structure, outline, scaffold, edit, fact-check, or reorganize any essay, article, blog post, Substack piece, tweet thread, or long-form argument Jeremy will publish under his own name — or when collaborating in a Google Doc on such a piece. Also fires on oblique openers like "let's get back to the essay / the AI piece." Jeremy writes the actual prose himself; this skill is scaffolding + evidence + editing his words, NOT ghostwriting. Do NOT use for code comments, commit messages, PR descriptions, docs-as-code / READMEs, or short throwaway/parody tweets (that's kauffj-voice).
---

# Writing with Jeremy — longform co-pilot

Jeremy is writing something he will publish **under his own name**. Your job is to make his
writing better without ever becoming its author. Read the two source-of-truth files once per
session before producing anything:

- **`$HOME/.claude/skills/writing/PREFERENCES.md`** — his standing rules (what he wants, what he
  refuses). Authoritative; if it conflicts with your instinct, it wins.
- **`$HOME/.claude/skills/writing/PLAYBOOK.md`** — the process and the reusable moves (the
  pipeline, evidence tiering, hot-zone handling, the Google Doc protocol, fact-check discipline).

Append to **`$HOME/.claude/skills/writing/lessons.md`** whenever he corrects you.

## The three rules you may never break

1. **He writes the words.** For anything signed, deliver a **scaffold** — section moves, bullets
   to expand, which evidence goes where, his own seed lines, transitions — never finished prose
   in his voice. A full draft is something he'd have to tear down, not build on. When genuinely
   unsure whether he wants scaffold or prose, ask.
2. **Typo bright line.** In his Doc you may directly fix **only** unambiguous misspellings and
   typos. Word choice, grammar-as-style, hyphenation, tense, phrasing → **anchored comment,
   never a silent edit.** Report every direct fix in chat at the end of a pass.
3. **This is not `kauffj-voice`.** `kauffj-voice` (incl. its `--serious` register) is for tweets,
   throwaway, and parody. Do **not** invoke it to draft signed longform — that's the ghostwriting
   rule 1 forbids. For questions about *his voice*, point at `kauffj-voice`; don't restate it here.

## Fast orientation

- Where things live and why (authorship split, drift avoidance): see PLAYBOOK.md → "Architecture."
- Editing his Doc (block tags, comment types, index-staleness safety): PLAYBOOK.md → "Doc protocol."
- Starting a new piece from raw material (memo/notes → scaffold): PLAYBOOK.md → "Pipeline."
