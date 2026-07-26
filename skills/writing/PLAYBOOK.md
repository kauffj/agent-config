# Writing Playbook

The process and reusable moves for helping Jeremy write longform. Generalized from the first
piece done this way (the "Where's the stuff?" AI essay, `~/projects/ai-post`). Read alongside
`PREFERENCES.md` (the rules) — this file is the *how*.

---

## Architecture: split by authorship, not by file type

The one rule that prevents drift — **no artifact has two authors.**

| Surface | Owner | Holds |
|---|---|---|
| The Google Doc | **Jeremy** | The manuscript (his prose), block tags, and the scaffold bullets he consumes and deletes as he writes. **Authoritative for the piece.** |
| The project folder (`~/projects/<piece>/`) | **Claude** | Evidence: project/source catalog, research & fact-check files, transcripts, the generated block manifest, dated Doc snapshots. |
| `~/.claude/skills/writing/` | **Shared, git-versioned** | This playbook, `PREFERENCES.md`, `lessons.md`. Cross-project. |

The scaffold is **not** duplicated into a local file. It lives in the Doc, where he writes. The
day you keep a second copy "for convenience," you've signed up to sync it forever — that's the
drift that motivated this whole system.

---

## Pipeline: raw material → finished piece

What worked, in order. Not every piece needs every stage; scale to the material.

1. **Capture.** He brain-dumps — often a walking voice memo. Transcribe locally on GPU
   (`whisper --model large-v3-turbo --device cuda`, ~2 min for 35 min of audio). Keep the raw
   audio; he may publish it beside the piece as proof of the method.
2. **Evidence dive.** Fan out parallel subagents over the relevant source (his `~/projects`, a
   corpus, prior work) to surface citation-worthy material. One focused task per subagent.
3. **Tier the evidence** into a catalog (see "Evidence tiering").
4. **Scaffold**, in the Doc: per section give **the move** (what it does), **bullets to expand**,
   **which evidence** backs it, **his own seed quotes** (line-ref back to the transcript), and a
   **transition**. Bullets, never prose.
5. **He drafts** — his words, section by section.
6. **Review by comment** (see "Doc protocol"). His replies feed `lessons.md`.

---

## Reusable moves

- **Evidence tiering.** Rank every candidate by *strength of proof for a specific claim*, not by
  how impressive it is. Tier 1 = leads that prove a thesis point outright; Tier 2 = strong
  support; Tier 3 = color. Mark which claim each one backs. A tiered list lets him pick fast.
- **Hot-zone list.** At the *start* of a piece, declare an explicit never-mention set — legal
  matters under active litigation (discovery risk), PII, donor names/amounts, secrets/tokens.
  Put it at the top of the catalog and honor it everywhere, including "generic" technique
  mentions. When unsure, describe the capability, not the internals.
- **Honest mixed-grade.** Include at least one real, self-critical assessment ("nails the rules
  of my voice, weak on content"). It's what separates the piece from hype and it's usually his
  best credibility move. Don't sand it off.
- **Meta-demo.** When the artifact can demonstrate its own thesis (an essay about AI leverage,
  produced via AI leverage), make that explicit and offer to show the pipeline. Strongest
  credibility move available.

---

## Fact-check discipline

- **Split fused claims.** A confident sentence often smuggles two claims, only one of which is
  true. (The AI memo fused "the scaling formula has few inputs" — true, Kaplan/Chinchilla — with
  "intelligence is a commodity" — a separate inference sourced to the leaked Google "We Have No
  Moat" memo. Asserting one *proves* the other is false and checkable.) Separate them; cite each.
- **Verify current status, not just position.** Re-check a person's role and tense before quoting
  them, not only what they believe. (David Sacks was cited as sitting AI czar; he'd stepped down
  ~Mar 2026.) A dated title is an error a reader catches instantly.
- **Flag the paywalled.** If a source 403'd or was paraphrased by an outlet, mark the quote
  "verify verbatim" rather than presenting it as confirmed.
- **Delegate fact-checks to subagents** and return sources (title, author, year, URL) so he can
  see the receipts, not just the conclusion.

---

## Doc protocol (Google Docs)

**Hard API constraint:** the Docs API **cannot create native "Suggesting"-mode edits.** It can
only write directly or add comments. The whole protocol is built around that.

### Block tags — `[[id]]`, semantic and position-independent
- Format: `[[tubs]]`, `[[sacks]]`, `[[moat]]` — lower-kebab, names *what the block is*.
- **Never encode position** in an id (`[[s2a]]` is wrong): the point is that moving a block
  doesn't invalidate its label. Cut/paste carries the tag with the content, so the label can
  never drift from what it names.
- Tag **only movable blocks** — anecdotes, evidence beats, argument beats. Plain connective
  paragraphs stay clean. He never types tags; he asks, you add them.
- Publish-strip: one find-replace, regex `\[\[[a-z0-9.-]+\]\]\s*` → empty.

### The manifest — `<piece>/BLOCKS.md`
`id → one-line summary → current section / order`. **Generated by reading the Doc, never
hand-maintained** (a hand-kept index is drift again). Regenerate on request. It lets you both
name blocks unambiguously and lets you propose a reordering as a list of ids without touching
the Doc.

### Rearranging
Default: **he drags** blocks in the Doc — native, he stays in control. If he asks you to reflow
mechanically from an id order, **snapshot the Doc first**; Docs revision history + the snapshot
are the rollback path.

### Comments — typed, one issue each, anchored to the exact span
Prefix every comment so they're skimmable and filterable:
`[fact]` · `[structure]` · `[cut]` · `[evidence]` · `[voice]` · `[q]`.
Use `addComment`; read his replies with `listComments`. Where he pushes back → `lessons.md`.

### The issue lifecycle (who does what)
1. **Claude raises** — one issue per comment, typed, anchored, with a concrete suggested fix.
2. **Jeremy acts** — revises if he agrees; replies if he wants to push back or discuss.
3. **Claude re-checks** on request ("sweep comments" / "check §N") — replies "✔ addressed" when a
   revision covers it, or follows up **once** if it doesn't. Never nag beyond one follow-up.
4. **Jeremy resolves** — the ✓ in the Docs UI is **his alone**. Resolving with NO text change =
   **won't-fix**: a decision, not an oversight. Never re-raise it; promote durable ones to
   `lessons.md` / `PREFERENCES.md`.

### State & sync (how the loop survives across sessions)
- **The Doc's comment store is the state database** — thread text, replies, and resolved status
  all live in the API. Re-derive open/closed from `listComments` on every sweep; never trust a
  local cache over it.
- **`<piece>/ISSUES.md` is the generated ledger** (like `BLOCKS.md`): comment ID → type → topic →
  status. Regenerated each sweep. Its irreplaceable job is **won't-fix memory** — resolved-without-
  change can't be distinguished from resolved-after-revision months later without it.
- **Before raising any new comment**, check the ledger for topic duplicates — especially wontfix
  rows. A fresh session independently re-noticing a vetoed issue must not re-raise it.
- **Evidence lives IN the Doc**, merged into the scaffold bullets he consumes while drafting
  (✓-marked when repo-verified) — no side companion file to sync. Deep sources (full URLs, long
  quotes) stay in the project's `research/` files, referenced from the bullets.

### Index-staleness safety (the main operational hazard)
- Always `getGoogleDocContent` immediately before any write — indices shift under edits.
- Prefer anchoring by unique text (`findAndReplaceInDoc`) over raw character indices.
- Write only when asked; never edit while he's typing.
- Snapshot before any multi-edit or structural operation.
