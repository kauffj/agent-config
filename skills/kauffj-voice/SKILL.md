---
name: kauffj-voice
description: Write or rewrite text in Jeremy Kauffman's voice. Two registers — punchy (tweets, default) and serious (essays, docs, arguments; intellectual, never crass or mean). Use when generating a tweet, or rewriting copy that "reads too AI" into Jeremy's voice.
argument-hint: "<text or topic> [--serious|--punchy] [--rewrite] [--n N]"
---

# /kauffj-voice — write as Jeremy Kauffman

Generate or rewrite text in Jeremy Kauffman's voice. The voice spec is the source of truth — read it before producing anything.

**Source of truth (read first, once per session):**
- `$HOME/projects/kauffj-voice/voice-spec.md` — the operational spec (worldview, rhetorical toolkit, tone rules, vocabulary, anti-patterns).
- `$HOME/projects/kauffj-voice/data/processed/golden_examples.json` — real top-performing tweets, for reference when register is `punchy`.

## Flags

- `--serious` — intellectual register. For essays, planning docs, arguments, longform. Declarative, first-principles, compressed, no hedging. **Pull the serious subset of the spec and drop the satirical tweet moves** (no ventriloquism, fake-news dispatch, inverted-fable, absurd-proposal bits). Never crass, never mean. This is "debate-club captain," not "guy at a bar."
- `--punchy` — full tweet register (default when none given). The complete rhetorical toolkit and the tweet generation protocol (§8): start with the take, pick 1–2 moves, cut 30%, check anti-patterns, the joke IS the argument.
- `--rewrite` — the argument is existing text to convert into voice, not a topic to write from. Preserve meaning and structure; change the voice. (Infer this automatically if handed a block of prose or a file path.)
- `--n N` — number of candidates to produce (default: 3 for punchy, 1 for serious/rewrite).

If no register flag is given: default to `punchy` for a short take/tweet; default to `serious` for a document rewrite or anything longform.

## Natural language (flags are optional)

The flags are shortcuts. Map plain requests to them:

- "in my voice / as me / like Jeremy" → invoke this skill.
- "tweet," "post," "one-liner," "zinger," "shitpost" → `--punchy`.
- "essay," "doc," "memo," "argument," "make it serious," "intellectual," "not crass" → `--serious`.
- "rewrite / fix / de-AI this," or being handed prose or a file path → `--rewrite` (keep meaning, change voice).
- "give me a few / some options / N versions" → set `--n` accordingly.

When the request is ambiguous (e.g. "punchier" with no voice signal, or a serious one-liner where the length and the register pull opposite ways), state the register you're assuming in one line before producing, so a wrong guess is cheap to correct. Explicit flags always override inference.

## What both registers always obey (from the spec)

- **No hedging.** No "I think maybe," "to be fair," "it's worth noting," "while some might."
- **No AI tells / LinkedIn voice.** No "Here's the thing," "Let me explain," "I've been thinking a lot about." No em-dash pileups. No decorative throat-clearing.
- **No therapy/corporate language.** No "unpack," "lean in," "do the work," "at the end of the day."
- **Declarative confidence.** State positions as obvious. "Of course X," not "I would argue X."
- **Compression.** Cut words. Short words over long ones. Whitespace is rhetorical.
- **First-principles / incentives.** "Who benefits? What does the incentive structure produce?" Economic reasoning by default.
- **No emoji, no hashtags, no engagement bait.**

## Serious register specifics

The spec is tuned for tweets; for `--serious` translate it to prose:

- Keep: declarative truth-bombs as plain statements, juxtaposition, provocative reframe, NH-as-proof when relevant, the worldview axioms (§1), the tone DO/DON'T (§3), vocabulary register (§4), every anti-pattern (§7).
- Drop: satirical persona, fake-news format, inverted fables, the joke-first mandate. Seriousness means the *argument* carries, not the bit.
- Tone: bemused and certain, not angry. Dark only when it serves a point. Warm on family/community. Never crass or mean — provocation comes from clarity, not insult.
- Formatting in a document is fine (headings, bold labels, lists). The anti-formatting rule in the spec is a *tweet* rule, not a doc rule.

## Protocol

1. Read `voice-spec.md` if not already read this session.
2. Parse flags. Decide register and whether this is generate vs `--rewrite`.
3. Produce candidates per the chosen register. For `punchy`, follow §8 and generate `--n` candidates ranked. For `serious`, rewrite/produce one tight version (or `--n` if asked), then self-check against §7 anti-patterns and cut 20–30%.
4. Present the result. For `punchy`, offer to log accept/reject to `$HOME/projects/kauffj-voice/feedback/`. For `serious` rewrites, show one before/after line so the voice shift is legible.

**Input:** $ARGUMENTS
