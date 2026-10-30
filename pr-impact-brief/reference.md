# Script reference

Run all of these from the root of the scikit-learn checkout, with
`required_permissions: ["all"]`. The default sandbox blocks writes to
`agents/`, which is a symlink to a sibling repository.

Script paths below are relative to this skill's directory, the one that
contains `SKILL.md` and this file. It differs by install location (for
example `~/.claude/skills/pr-impact-brief/`, `~/.cursor/skills/pr-impact-brief/`,
or a `.claude/skills/` or `.cursor/skills/` folder inside a repository), so
resolve it from where you loaded `SKILL.md` and substitute it for `<skill-dir>`.

## `scripts/fetch_open_prs.py`

Fetches every open PR into a JSON cache via GraphQL. Cheap: the whole
repository costs about 23 rate-limit points out of 5000.

```
python3 <skill-dir>/scripts/fetch_open_prs.py
       [--repo OWNER/NAME]        default scikit-learn/scikit-learn
       [--cache PATH]             default agents/impact/.cache/open-prs-<repo>.json
       [--max-age-hours N]        reuse cache if younger than N (default 24)
       [--force]                  refetch regardless of age
       [--quiet]
```

Safe to call at the start of every session: it prints `cache fresh (Nh old)`
and exits without network access when the cache is young enough.

Page size is fixed at 25. Larger pages exceed GitHub's node limit and return an
empty body rather than an error, so do not raise it.

## `scripts/filter_prs.py`

Applies the worklist filter and draws the next PR.

```
python3 <skill-dir>/scripts/filter_prs.py
       [--featurey | --all]       narrow to public-surface additions, or widen
       [--pick N] [--seed N]      draw N unbriefed PRs at random
       [--brief-dir PATH]         default agents/impact, searched recursively
       [--include-briefed]        do not skip PRs that already have a brief
       [--json]                   machine-readable, includes the brief_path to write
       [--counts]                 print filter cardinalities and exit
       [--cache PATH] [--repo OWNER/NAME]
```

Modes, with cardinalities as of July 2026:

| Mode | Meaning | Count |
| --- | --- | --- |
| default | touches scikit-learn source, no core-dev has commented or reviewed | 106 |
| `--featurey` | the above, restricted to `ENH`/`FEA`/`API`/`PRF` or a feature label | 22 |
| `--all` | every human non-draft PR | 465 |

`--json` is the form to use in a session: it carries `brief_path`, so the
filename is decided by the script and stays stable for a given PR. `brief_path`
always points at the top level of `--brief-dir`, which is where new briefs go.

The briefed check is recursive, so a brief the user has filed under
`agents/impact/done/` still counts and its PR is not drawn again. Archiving is
therefore safe; only deleting a brief puts a PR back in the draw.

Exit code 3 means the worklist is exhausted — every PR in that mode already has
a brief. Say so rather than widening the mode unasked.

## `scripts/pr_context.py`

Builds the deterministic context bundle for one PR.

```
python3 <skill-dir>/scripts/pr_context.py NUMBER
       [--repo OWNER/NAME]
       [--out PATH]               write to a file instead of stdout
       [--json]                   raw bundle instead of rendered markdown
       [--diff-lines N]           cap on diff lines (default 2000, 0 for none)
       [--no-prior-art]           skip the history search (saves ~12s)
       [--cache PATH]             open-PR cache, for overlap detection
```

Sections produced:

1. **Metadata and PR body.**
2. **Changed files** with per-file line counts.
3. **Public API surface delta** — base and head of each changed `.py` file are
   parsed with `ast` and compared. Reports new and removed public callables, new
   and removed parameters, and changed defaults. `__init__` counts as public
   because for an estimator its parameters *are* the public API.
   Caveat: per-file and per-class, so it does not follow inheritance. A method
   moved to a new base class appears as both removed and added. Check the diff
   before calling anything removed.
4. **Linked issues** with full discussion, from `closingIssuesReferences` plus
   `Fixes`/`Closes`/`Resolves` in the body. Usually the only place user impact
   is stated at all.
5. **PR discussion.**
6. **Prior art** — title searches of repository history for identifiers derived
   from the PR. Identifier-like terms (containing `_` or a capital) get up to 25
   hits; bare English words are capped at 4 most recent, with a note giving the
   true total, because a word like "categorical" matches 126 titles and buries
   the one result that matters. Throttled to 2.5s between queries, at most 5
   queries, to stay under the search API's 30/minute limit. Search failures are
   reported inline rather than swallowed.
7. **Other open PRs touching the same files** — read from the local cache at
   zero API cost. This is how overlapping and duplicate PRs get noticed.

Take the whole diff into account, but do not review it. Correctness and style
are out of scope.
