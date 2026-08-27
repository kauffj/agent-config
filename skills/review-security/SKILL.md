---
name: review-security
description: Review changed code for authorization gaps, input validation failures, data exposure, and injection vulnerabilities
---

# Review Security

Review code changes for security vulnerabilities.

Apply the `review-pr` skill in worktree mode for the current directory with
`--reviewers security`. If the invocation includes a base branch, also pass it
as `--base <branch>`; otherwise let `review-pr` resolve the actual remote
default. Present its security findings verbatim.

The `review-pr` skill is the single owner of changed-file gathering, default-
branch resolution, empty-diff handling, and reviewer delegation. Do not
reimplement those operations here.
