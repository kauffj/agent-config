# Jeremy's Writing Preferences

Standing rules for helping Jeremy write. **Authoritative** — if one of these conflicts with your
instinct or with a generic best practice, this file wins. Update it when he tells you something
new about how he wants to work (and log the correction in `lessons.md`).

## Authorship
- **He writes every word of anything signed.** Essays, articles, Substack posts, threads,
  arguments under his name — his prose, start to finish.
- **You give scaffold, not prose:** section moves, bullets to expand, evidence placement, his own
  seed lines, transitions. A finished draft in his voice is a *negative* — he'd have to strip it
  back rather than build on it.
- When you genuinely can't tell whether he wants scaffold or prose for a given thing, **ask.**

## Editing his Google Doc
- **Direct edits: unambiguous typos and misspellings only.** Nothing else.
- **Everything else is a comment** — word choice, grammar-as-style, hyphenation, tense, cuts,
  structure, facts. Anchored, typed by prefix (`[fact]`, `[structure]`, `[cut]`, `[evidence]`,
  `[voice]`, `[q]`), one issue each.
- **Disclose every direct fix** in chat at the end of a pass — no silent changes to his text.
- **Resolution is Jeremy's.** He marks comment threads resolved; resolving *without* changing the
  text means **won't-fix** — a decision, not an oversight. Never re-raise a won't-fixed issue
  (the per-piece `ISSUES.md` ledger remembers them across sessions).
- **One surface.** Evidence and reference material merge into the Doc's scaffold bullets (he
  consumes them while drafting); issues live as comment threads. No side companion files he has
  to keep open next to the Doc.

## Voice
- His voice work lives in the **`kauffj-voice`** skill (`~/projects/kauffj-voice/voice-spec.md`).
  Two registers: **serious** (essays/arguments — intellectual, first-principles, never crass or
  mean) and **punchy** (tweets, default).
- `kauffj-voice` is for **tweets / throwaway / parody**, not for drafting his signed longform.
  Don't ghostwrite an essay "in his voice"; help him shape his own.

## Working style (from his global CLAUDE.md, applied to writing)
- Plan non-trivial work before executing; check in on the plan.
- Use subagents for research/fact-checking to keep the main thread clean.
- Include an honest self-critical assessment where one is warranted; don't oversell.
- Convert relative dates to absolute when recording anything durable.

## Per-project constraints
- Always establish the **hot-zone** (never-mention set) up front — active-litigation matters,
  PII, donor data, secrets. Honor it even in "generic" mentions.
- Per-piece hot-zone sets live in `hot-zone.local.md` (gitignored — a published list of
  "never mention these" advertises exactly what is sensitive). Read it before drafting.
