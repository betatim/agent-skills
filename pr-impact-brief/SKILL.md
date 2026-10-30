---
name: pr-impact-brief
description: Assemble source-linked evidence on whether a scikit-learn PR is worth carrying, one PR at a time, ending in the questions the decision hinges on and never in a verdict. Use when the user says "let's review some PRs for worth", asks to review or triage PRs for worth or impact, asks whether a PR is worth it or worth merging or worth carrying, or asks for an impact brief on a PR number or URL.
---

# PR Impact Brief

Walk one unattended scikit-learn PR at a time and present the evidence a maintainer needs to answer **"should we carry this?"** — then stop. The human decides.

This skill exists because the cost side of a change is invisible by default. A PR that is small, correct, and green still adds permanent public surface, consumes review time, and displaces bigger work. Nobody asks whether it is worth that, so the question goes unanswered for years.

## Hard rules

These are not style preferences. Violating any one of them defeats the purpose of the skill.

1. **Never write to GitHub.** No comments, no labels, no reviews, no closing, no reopening. `gh` is read-only here: `view`, `api` with `-X GET`, `pr diff`, `search`. Never `gh pr comment`, `gh pr close`, `gh issue edit`, or any `-X POST/PATCH/PUT/DELETE`.
2. **Never state a verdict or recommendation.** Not in the brief, not in the follow-up discussion, not when asked directly. If the user asks "would you merge it?", answer by naming what is still unresolved. A brief that concludes has failed, because the conclusion is the user's to reach.
3. **Every factual claim carries a source** — a URL, or a file path with a line number. The one exception is the reach bucket, which is explicitly an estimate and is labelled as such.
4. **Unsourced statements go in the `Unsupported claims` section.** If a claim cannot be sourced and cannot be dropped, it gets listed there. This makes the sourcing rule visible by construction rather than dependent on the reader's vigilance.
5. **Do not review the code.** Correctness, style, and test coverage are out of scope. Other tools do that. The only question here is whether the change is worth carrying.
6. **Do not fill gaps with plausible prose.** "None found" and "could not determine" are valid, informative answers. A confident paragraph about an issue you did not read is worse than an admission.
7. **Never relay the PR's own claims as findings.** The author's description of what is broken, who is affected, and why it matters is a claim to check, not evidence. Check every load-bearing one against the source. In the pilot this changed the conclusion on three PRs out of four: a "security fix" turned out to guard a branch unreachable since Python 3.11.4, a "docs fix" turned out to correct text that was already accurate, and a workaround a core dev had endorsed turned out to be broken.

## Permissions

The default sandbox blocks writes outside the workspace, and `agents/` is a symlink to a sibling repository. So:

- Run every script in `scripts/` with `required_permissions: ["all"]`.
- Write brief files with `required_permissions: ["all"]`, or via the file-writing tool if it has access.

Scripts need network access for `gh`, which the allowlist already covers.

## Scripts

Deterministic work belongs in these scripts, never in the model. They own all GitHub access and everything countable.

| Script | Purpose |
| --- | --- |
| `scripts/fetch_open_prs.py` | Paginated GraphQL fetch of every open PR into a JSON cache |
| `scripts/filter_prs.py` | Applies the worklist filter; draws the next PR at random |
| `scripts/pr_context.py` | Per-PR context bundle: diff, linked issues, public-surface delta, prior art |

Paths are relative to this skill's directory (the one containing this file), wherever it is installed. Run them as `python3 <skill-dir>/scripts/<name>.py` from the root of the scikit-learn checkout. Full usage for each is in [reference.md](reference.md).

## Workflow

1. **Ensure the worklist.** Run `fetch_open_prs.py`; it refreshes only if the cache is stale, so it is cheap to call every session.
2. **Pick a PR.** Run `filter_prs.py --pick 1`, which draws at random and skips anything already briefed. If the user named a specific PR, skip the draw.
3. **Gather context.** Run `pr_context.py <number>`. Read the linked issue's **labels** first: a `Needs Decision` or `Needs Triage` label means the project has formally recorded that the worth question is open, which outranks everything else in the brief. `AGENTS.md` also forbids contributing against such issues.
4. **Read scikit-learn's source.** This is the part no script can do, and the reason this is a skill. Two jobs here, and the second is easy to skip:
   - Answer the counterfactual. Does a sibling estimator already take this parameter, does an existing composition already achieve it? Cite file and line.
   - Verify the PR's central claim. Find the code the PR says is wrong, or the code path it says is vulnerable, and confirm it behaves as described. Check the version gate on any compatibility shim, and check whether a validating call upstream already makes the failure unreachable.
5. **Write the brief** to `agents/impact/<number>-<slug>.md`, before presenting it.
6. **Present it** in chat.
7. **Stay on the PR** for follow-up questions.
8. **Advance** only when told to.

Steps 5 through 8 are detailed under [Session loop](#session-loop).

## Brief template

Write to `agents/impact/<number>-<slug>.md`. Take `<slug>` from `filter_prs.py --json`, which supplies `brief_path`, so a given PR always maps to the same filename. Finished briefs live in `agents/impact/done/`, but the user files them there — see [Resuming](#resuming).

Keep it to roughly a page. Omit a section entirely when it has nothing to say, rather than padding it.

```markdown
# PR #NNNNN: <title>

<author> (<association>) | opened YYYY-MM | +A/-D across F files | <labels>
Kind: public-surface addition | bug fix | performance | API change |
      documentation correctness fix | documentation consistency edit |
      security hardening

## What it changes

Two to four sentences, in terms of observable behaviour rather than
implementation. Cite the files and line numbers.

## Reach — ESTIMATE, NOT SOURCED

Bucket: most users | a common workflow | a niche workflow | one reporter

Affected population: who exactly hits this, under what conditions. This is
the stated basis for the bucket, so that a reader who knows the domain can
falsify the bucket instead of having to trust it.

## Severity

For bug-shaped PRs. One of: silently wrong results | crash or exception |
wrong documentation | annoyance with a workaround | cosmetic. Link the
reproducer, the failing test, or the issue comment that establishes it.
Omit this section for pure feature additions.

Two kinds fall outside that ladder; say so rather than forcing a rung:

- Security. Separate the theoretical impact from the exploitability, and
  source the exploitability. A scanner's severity rating is the scanner's
  opinion about a pattern, not a finding about this code.
- Documentation. Distinguish text that is *wrong* from text that is merely
  *inconsistent*. If the current wording misleads nobody, the honest rung is
  cosmetic, however plausible the PR's framing.

## Counterfactual — can users get this today without us?

For features: name the existing scikit-learn API that already covers it,
with file and line, or show the user-side workaround as code, or name the
third-party package. If nothing covers it, say so.

For fixes: is there a workaround, and what does it cost the user — a few
lines, a subclass, or reimplementing an estimator.

"None found" is a valid and informative answer. Do not invent a workaround
you have not checked against the source.

## Cost

- Public surface added: new parameters, classes, methods, modules, with file
  and line. Take this from the surface delta, not from the diff by eye.
- Backward compatibility: changed defaults, changed behaviour for existing
  callers, deprecations. Changed defaults are the highest-risk item here.
- Review effort: diff size, Cython involvement, new dependency, whether it
  touches shared core code many estimators rely on.
- Permanent maintenance: what has to keep working forever once merged.

## Prior art

Earlier issues and PRs proposing the same thing, and what happened to them.
Links and outcomes only. A previous attempt closed unmerged is the single
most decision-relevant fact this tool can surface — state it plainly and let
the reader draw the conclusion.

Watch for a *cluster* of near-identical titles: several attempts at one change
closed within weeks of each other means the project keeps deciding something,
and this PR's fate probably turns on that decision rather than on its diff.
Note that `pr_context.py` gives you titles, dates and merged-or-not, but not
close *reasons* — so the reason belongs in a hinge question, not in a claim.

Also note any other open PR touching the same files, since two PRs that
overlap are one decision rather than two.

## What this hinges on

Two or three specific questions a maintainer must answer to decide this.

They must be specific to this PR. "Is this worth the maintenance cost?" is
not a hinge question, it is a restatement of the problem. "Does `sample_weight`
on an encoder have a defined meaning when categories are unseen?" is one.

No recommendation, no confidence score, no verdict.

## Unsupported claims

Every statement above that has no link or file reference. The reach bucket
always belongs here. If this section is empty and reach is stated, something
has been dressed up as evidence.
```

### Filling the four dimensions

**Reach** is the only unsourced field. Estimate it, label it, and name the population it applies to. Do not smuggle in confidence by citing download counts or code-search totals: per-API reach is not measurable this way. GitHub code search returns roughly 2,500 import matches for the obscure `SelectFwe` and only 17x more for `LogisticRegression`, because the counts are approximate and dominated by vendored copies. Reactions are near-empty on this repository too — commonly zero on issues with ten comments. Neither is evidence.

**Severity** comes from the linked issue and the reproducer, not from the PR author's framing. "Silently wrong results" is a much stronger claim than "raises an exception" and needs the stronger evidence.

**Counterfactual** is the dimension that most often decides the question, and the only one that requires reading scikit-learn's source. Actually go and look: does the sibling estimator already take this parameter, does `Pipeline` plus an existing transformer already compose it, is it three lines in user code. This is the work; do not skip it and assert an answer.

When the workaround is runnable, **run it** rather than describing it. Use the Python environment that has this checkout installed in development mode. If the repository's agent instructions (`AGENTS.md`, `CLAUDE.md`, `.cursorrules` or similar) say how to activate it, follow them; otherwise ask the user rather than guessing. Before trusting any output, confirm with `python -c "import sklearn; print(sklearn.__file__)"` that it imports from the checkout, not from some other install, because a shell where activation silently failed still runs Python. Then execute the workaround and paste the real output. If you cannot get a working environment, say in the brief that the workaround was not run. A workaround that a core developer posted years ago may since have rotted, and only running it will tell you. In the pilot, the endorsed workaround for #29952 turned out to reject string labels outright and to report R² in place of accuracy — neither of which was visible from reading the thread, and both of which changed the picture.

Check upstream too. If a newer version of Python, NumPy or SciPy already fixes it, the counterfactual is "upgrade", and the PR is only relevant for the window of versions the project still supports. Get that window from `requires-python` in `pyproject.toml`, and get the exact fix version from the upstream changelog rather than from memory.

**Cost** is largely mechanical — take it from the surface delta. Two judgment parts: what the project has to keep working forever, and whether the PR bundles a change with a *wider* blast radius than its title suggests. A feature PR that also renames an existing public parameter is two decisions, and the reader should be told they can be separated.

Weigh maintenance against remaining lifetime. Code behind a version shim expires when the shim does, so a compatibility backport with one release cycle left is a different proposition from a permanent estimator, even at the same line count.

### Apparent engagement

The worklist selects PRs no core developer has touched, but such a PR can still carry approving reviews from non-core contributors and bots, and so look attended to. If a review carries `APPROVED` from an author whose association is not `MEMBER` or `OWNER`, note it — the reader is otherwise likely to read the thread as "already reviewed" when nobody with merge authority has looked.

## Session loop

This runs as a conversation, one PR at a time. Never brief several PRs in one turn.

### Starting

Triggered by things like "let's review some PRs for worth", "review PRs for impact", or "is #34204 worth it". Then:

1. Run `fetch_open_prs.py`. It no-ops on a fresh cache, so this is cheap.
2. Say how large the worklist is and how many already have briefs, in one line. This is the only status output the user needs.
3. Go straight to the first PR. Do not ask which mode to use or how many to review; default mode, one PR, and adjust if asked.

If the user named a specific PR, skip the draw and brief that one, whether or not it is in the worklist.

### Picking

Run `filter_prs.py --pick 1 --json`. The draw is random and skips anything already briefed — do not impose an ordering of your own, and do not seed it unless asked, because the point of randomness is an unbiased view of what is actually in the worklist.

On exit code 3 the worklist is exhausted. Say so and stop. Do not widen to `--all` or `--include-briefed` without being asked.

### Briefing

Run `pr_context.py <number>`, read scikit-learn's source for the counterfactual, then **write the brief file before presenting it**. Writing first means an interrupted session loses nothing.

Then present it in chat. Present the substance — do not just say "written to <path>" and make the user open a file. Do not re-summarise the brief after presenting it either.

### Discussing

Stay on this PR. Answer follow-ups by going and looking: read more source, fetch the diff of a prior attempt, pull an issue's full discussion. Do not answer by rephrasing what the brief already said, and do not manufacture a new consideration to seem responsive — "the brief covers what I found; the open question is still X" is a fine answer.

Expect the hardest pushback on the counterfactual, and be ready to show the actual lines that support it.

If the user asks for a verdict — "so would you merge this?", "is it worth it?" — do not give one. Answer with what is still unresolved and what would settle it. This will feel unhelpful; it is the design. The user chose it deliberately so that the brief informs their judgment instead of anchoring it.

If the user states their own conclusion, do not record it anywhere and do not offer to draft a comment. There is no verdict capture in this tool.

### Advancing

Only on an explicit "next", "another", or equivalent. Then draw again. A follow-up question is not a request to advance.

### Resuming

The presence of a brief file anywhere under `agents/impact/` is the entire state. There is no separate bookkeeping file.

Two directories, one meaning:

- `agents/impact/` — briefs still in play. **Always write new briefs here**, never straight into `done/`.
- `agents/impact/done/` — briefs the user has finished with. Filed by hand, so the folder does not fill up.

Both count as briefed, so a PR filed under `done/` is not drawn again. `filter_prs.py` searches recursively for this reason; do not "fix" it back to a flat glob.

- "Show me that one again" means reading the existing file wherever it sits — check `done/` too before concluding a brief does not exist. Do not regenerate it.
- Never move a brief into `done/` yourself. That is the user's signal that they are finished, and it is not yours to give.
- "Start over" or "review those again" means **deleting** the files, and only the ones named. Moving a brief no longer resets anything. Confirm before deleting.
- A brief older than a few weeks may be stale; note the file's date rather than silently trusting it.

## Style rules

Follow these conventions (shared with the pr-walkthrough skill):

- Link to code with relative markdown links from the brief's location, e.g. `[_encoders.py](../../sklearn/preprocessing/_encoders.py)`, and put line numbers in the prose next to the link. Do not use `vscode://file/` URIs or `#L<n>` fragments; neither renders in Cursor.
- Be concise. Every line earns its space. A brief is a page, not an essay.
- No flattery, no filler, no marketing tone. No emoji.
- Show code only when prose cannot carry the point — most often when demonstrating a counterfactual workaround.
- Prefer specifics over hedged generalities. "Affects users who pass `sample_weight` to `OneHotEncoder.fit`" beats "affects some users".
