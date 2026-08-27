---
name: review-simplicity
description: Review changed code for unnecessary complexity, complection, premature abstraction, and incidental complexity
---

# Review Simplicity

Review code changes for unnecessary complexity.

Apply the `review-pr` skill in worktree mode for the current directory with
`--reviewers simplicity`. If the invocation includes a base branch, also pass
it as `--base <branch>`; otherwise let `review-pr` resolve the actual remote
default. Present its simplicity findings verbatim.

The `review-pr` skill is the single owner of changed-file gathering, default-
branch resolution, empty-diff handling, and reviewer delegation. Do not
reimplement those operations here.
