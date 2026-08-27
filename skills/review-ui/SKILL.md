---
name: review-ui
description: Review changed frontend code against UI design principles (Nielsen heuristics + Atomic Design)
---

# Review UI Changes

Review frontend code changes against the UI design principles.

Apply the `review-pr` skill in worktree mode for the current directory with
`--reviewers ui`. If the invocation includes a base branch, also pass it as
`--base <branch>`; otherwise let `review-pr` resolve the actual remote default.
Present its UI findings verbatim. If the centralized classifier finds no
frontend files, report that result and stop.

The `review-pr` skill is the single owner of changed-file gathering, default-
branch resolution, frontend classification, empty-diff handling, and reviewer
delegation. Do not reimplement those operations here.
