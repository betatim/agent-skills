---
name: pr-walkthrough
description: Produce a structured walkthrough of a GitHub PR ordered by causal dependency. Use when the user asks to walk through, analyze, or understand a PR's changes.
---

# PR Walkthrough

Generate a causal-dependency-ordered walkthrough of a GitHub PR and write it to `agents/reviews/` in the repository.

This skill is scoped to PRs in the current repository. Cross-repo PRs are out of scope.

## Workflow

### 1. Check out the PR locally

**STOP and ask before switching branches.** Check the current branch with `git branch --show-current`. If it is not already the PR branch, tell the user which branch they are on and ask for confirmation before running `gh pr checkout`. The user may have uncommitted work or prefer to stay on their current branch.

Run `gh pr checkout` with `required_permissions: ["all"]` -- it needs to write tracking info to `.git/config`, which the default sandbox blocks.

A local checkout is needed so that:
- Source files can be read in full (not just the diff).
- The walkthrough can include relative links that open directly in the editor.

**Do NOT switch back to the original branch when done.** The review file contains relative links and line numbers that reference the PR branch. Switching away would break them. Leave the user on the PR branch after writing the walkthrough.

### 2. Gather context

Run these in parallel via `gh` CLI (if `gh` is not on PATH, check for a conda or virtual environment that provides it):

- PR metadata: `gh pr view <number> --json title,body,url,state,headRefName,baseRefName,labels`
- Full diff: `gh pr diff <number>`
- Commits: `gh pr view <number> --json commits`
- Changed files: `gh pr view <number> --json files`
- Comments and reviews: `gh pr view <number> --json comments,reviews`
- Inline review comments: `gh api repos/{owner}/{repo}/pulls/{number}/comments`

Derive the primary context from the PR title, body, and commit messages -- these are always the starting point.

If the PR body references issues (`Fixes #N`, `Closes #N`, or links), fetch those for additional context. If an issue is a subtask of a tracker/epic, fetch the parent to understand the broader motivation. Fetch comments/discussion on all linked issues and referenced PRs as well -- they often contain relevant rationale.

If there is no linked issue, rely on the PR title, body, commit messages, and review discussion to derive context.

### 3. Read relevant source files

Don't just rely on the diff -- read enough surrounding code to understand the before/after:

- For each changed **function**: read the full function body, read its callers (to understand impact), and read the functions/classes it calls (to understand dependencies).
- For each changed **class**: read the full class, its parent classes, and its descendants (to understand inheritance effects).
- For each changed **block** (e.g. top-level statements, config): read the full enclosing file section and imports.

This context is essential for assessing whether the changes are correct and complete.

### 4. Identify the dependency levels

Classify every change into levels by causal dependency:

- **Level 0** is the primary goal -- changes directly implementing the PR's stated purpose (the feature, the bug fix, the refactor).
- **Levels 1 through N** form a ripple chain: each level contains changes that were caused by or needed for the previous level. Follow the chain as deep as it goes. Level 1 is changes required by Level 0, Level 2 is changes required by Level 1, and so on. There is no fixed depth -- some PRs have one ripple level, others have five.
- After the ripple chain, two special categories:
  - **Cleanup**: dead code removal, unused import deletion, or other simplification enabled by the primary change.
  - **Tests/CI/Docs**: test additions/removals, CI config, doc updates, warning suppression caused by any of the above.

Not every PR will have all categories. Some PRs are Level 0 only. Skip what doesn't apply. The key principle: **start with the change the author wanted to make, then follow the ripple effects outward.**

Within each level, group related file changes together. Do not walk through the PR file-by-file -- organize by logical concern. Multiple files that participate in the same change belong in the same section.

### 5. Write the walkthrough

Output file: `agents/reviews/<pr-number>-<short-slug>.md`

Structure:

```
Date: <yyyy-mm-dd>

# PR #<number>: <title>

**Author:** <login> | **Branch:** <head> -> <base> | **Status:** <state>

---

## Context

Why the PR exists. Derive from the PR title/body/commits, linked issues,
and review discussion. Keep this short -- 1-2 paragraphs max.

---

## Walkthrough

### Level 0: <one-line summary>

Describe the primary changes. Cite files, show representative before/after
snippets. Explain the pattern, not every instance. Group related file
changes together.

### Level 1: <one-line summary>

What Level 0 forced. Explain *why* each change was necessary.

### Level N: ...

Continue until all changes are covered.

Include a mermaid dependency diagram if there are 3+ levels.

---

## Positives

(Include only when there are substantive points to make.)

Numbered list. Focus on architectural wins, consistency improvements,
net-negative LOC, test quality. Be specific, not generic.

---

## Edge Cases and Concerns

(Include only when there are substantive points to make.)

Numbered list. Each item should name the specific scenario, which code
is affected, and why it matters. Distinguish between "likely problem"
and "worth noting but probably fine."
```

### 6. Style rules

- Be concise. Every paragraph should earn its space.
- Link to code using relative markdown links from the review file's location (e.g. `[_sorting.pyx](../../sklearn/utils/_sorting.pyx)`). Do NOT use `vscode://file/` URIs or `#L<line>` fragments -- neither works in Cursor's markdown viewer. Instead, mention line numbers in the text next to the link (e.g. `[_sorting.pyx](../../sklearn/utils/_sorting.pyx) line 63`).
- Show code snippets only when they clarify something the prose can't.
- Don't repeat the same pattern at every call site -- describe the pattern once, then list where it applies.
- No flattery, no filler phrases, no marketing tone.
- Use mermaid diagrams for dependency relationships when they help.
