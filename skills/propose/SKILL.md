---
name: propose
description: Produce an approved implementation proposal — plan, simplicity-review the plan, checkpoint with user. Returns a plan ready to implement.
argument-hint: "<description of what to build> [--workspace <name>]"
disable-model-invocation: true
---

# /propose — align before you build

Plan a non-trivial change, get it reviewed against simplicity principles, then confirm with the user. Returns an **approved plan** that a downstream skill (or you) can implement.

Use this when the work is big enough that you want alignment before writing code. `/feature` calls this as Step 1; you can also call it standalone for a refactor, a bug-fix approach, a spike design, etc.

**Reference files:**
- `$HOME/.claude/hickey-principles.md` — simplicity criteria for the design review

---

## Parse `$ARGUMENTS`

Split `$ARGUMENTS` into the description and optional `--workspace <name>`.

If `--workspace <name>` is provided, fetch the workspace record so the plan targets the right worktree:

```bash
node $HOME/.claude/lib/workspace.mjs get <NAME>
```

Use its `worktreePath` as the working directory for the Plan agent. If no workspace, use the current working directory.

Also load the project profile so the plan knows the stack:

```bash
node $HOME/.claude/lib/project.mjs load
```

Extract `$STACK`, `$DEPLOY_MODEL`, `$BUILD_CMD`.

---

## Step 1: Plan

Launch an Agent (subagent_type: "Plan") with:

> You are a senior software architect planning a change.
>
> **Working directory:** `$CWD` (use this for all file exploration)
> **Tech stack:** $STACK
> **Deploy model:** $DEPLOY_MODEL
>
> Request: "$DESCRIPTION"
>
> **Phase 1 — Describe the current state:**
> Explore the codebase to understand the area this change touches. Document what exists today — relevant files, patterns, data models, and UI. Do NOT jump to solutions yet.
>
> **Phase 2 — Plan the change:**
> Based on your understanding, produce a plan with:
> 1. **Summary** — one paragraph describing what will be built
> 2. **Approach** — step-by-step implementation plan
> 3. **Files to create/modify** — list each file and what changes
> 4. **DB changes** — any new tables, columns, or migrations (if none, say "None")
> 5. **UI components** — new components needed, following existing patterns (if none, say "None")
> 6. **Affected URLs** — routes that will be added or changed (if not applicable, say "None")
> 7. **Acceptance criteria** — how will we verify this works? List specific observable outcomes.
> 8. **Unknowns/risks** — anything you're unsure about or that could go wrong
>
> Be specific. Reference existing patterns you find in the codebase.

Capture the plan as `$PLAN`.

---

## Step 2: Simplicity review of the plan

Launch an Agent to review the PLAN (not code) against Hickey's simplicity principles.

Prompt:

> You are a design reviewer focused on simplicity and avoiding complection.
>
> First, read `$HOME/.claude/hickey-principles.md` to load your review criteria.
>
> Then review this plan:
>
> [INSERT $PLAN]
>
> Evaluate:
> - Is anything being complected that should be separate?
> - Are there easy-over-simple tradeoffs being made?
> - Are there unnecessary abstractions or premature generalizations?
> - Could the approach be simpler while meeting the requirements?
> - Are there "functions doing two things" in the proposed design?
>
> Output one of:
> - **APPROVED** — the plan is sound, with any minor notes
> - **BLOCKED** — specific issues that must be addressed, with concrete suggestions
>
> Be rigorous but practical. Don't block for theoretical concerns.

If **BLOCKED**, feed the feedback back into Step 1 (re-plan with the review appended). Max 2 re-plan iterations. After 2 rejections, present the situation to the user and ask how to proceed.

---

## Step 3: User checkpoint

Present to the user:
1. The approved plan
2. The design-review notes
3. The working directory (`$CWD`)

Use `AskUserQuestion` to ask: "Does this plan look good? Reply 'yes' to proceed, or provide feedback to adjust."

If the user provides feedback, incorporate it and re-run from Step 1 (or just adjust the plan directly if changes are minor).

**Do not return without user approval.**

---

## Output

On approval, save the plan for resume / downstream use:

```bash
mkdir -p .claude/plans
echo "$PLAN" > .claude/plans/<slug>.md
```

If `--workspace <name>` was provided, also store the plan in the workspace record's `pipeline`:

```bash
node $HOME/.claude/lib/workspace.mjs update <NAME> "$(jq -n --arg p "$PLAN" '{pipeline: {plan: $p}}')"
```

Print a machine-parseable block so callers can consume the result:

```
PROPOSAL_APPROVED
planPath=.claude/plans/<slug>.md
workspace=<NAME or empty>
```

Also print the plan itself so the user sees the final version.
