---
name: explore
description: Deep codebase research in isolated context — investigate a question thoroughly without cluttering the main conversation
---

# Deep Codebase Exploration

Delegate the investigation to an available exploration subagent so its working
context stays isolated. If this skill is already running in such a subagent,
continue there. If delegation is unavailable, investigate in the current
context and report that limitation.

Be thorough — check multiple files, trace call chains, read tests, and look at git history when relevant.

Treat the text supplied with the invocation as the question.

## Approach

1. **Understand the question** — what specifically needs to be answered?
2. **Find entry points** — search for relevant files, functions, types, routes
3. **Trace the chain** — follow imports, function calls, data flow
4. **Check edges** — look at tests, error handling, edge cases
5. **Check history** — if relevant, look at recent git commits touching these areas

## Output

Structure your findings as:

### Answer
Direct answer to the question in 2-3 sentences.

### Evidence
- File references with line numbers
- Code snippets that prove your answer
- Data flow diagrams if relevant (text-based)

### Related
Anything surprising or noteworthy you found along the way that the user should know about, even if they didn't ask.
