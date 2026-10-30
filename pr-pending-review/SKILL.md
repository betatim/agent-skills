---
name: pr-pending-review
description: Post inline comments on a GitHub pull request as a PENDING (draft) review in the user's name, which they then edit and submit in the browser. Use when the user wants comments placed on specific lines of a PR diff, e.g. "put these as review comments on PR #N", "add inline comments explaining the changes", "post my review comments as a draft review". Works for the user's own PRs and for PRs they are reviewing. Only posts comments the user already has; it never writes the comments itself and never submits the review.
---

# pr-pending-review

Posts a list of inline comments to a PR as an unsubmitted (pending) review owned
by the authenticated `gh` user. The user refreshes "Files changed", edits,
deletes or adds comments, and submits the review themselves.

## Workflow

1. **Get the comments.** They come from the conversation: explanations of the
   user's own changes, findings from a review, and so on. This skill doesn't decide
   what to say. Agree the text with the user before posting.
2. **Write them to a JSON file** (scratchpad), as a list of objects:
   ```json
   [
     {"path": "src/x.py", "line": 42, "body": "Single line on the new side."},
     {"path": "src/x.py", "start_line": 40, "line": 42, "body": "A range."},
     {"path": "src/x.py", "line": 7, "side": "LEFT", "body": "On a deleted line."}
   ]
   ```
   - `line`/`start_line` are file line numbers, not diff positions.
   - `side` is `RIGHT` (new file, the default) or `LEFT` (old file).
   - Only lines in the diff (added, deleted or context) can be commented on, and
     a range must stay within one diff hunk.
3. **Dry run first**, and fix any `INVALID` lines:
   ```bash
   python ~/.claude/skills/pr-pending-review/scripts/post_pending_review.py \
       --repo OWNER/NAME --pr N comments.json --dry-run
   ```
4. **Post** the same command without `--dry-run`. This is outward-facing, so only
   post once the user has approved the comments. Then tell the user to refresh
   the PR's "Files changed" tab and submit.

## Behaviour

- **All or nothing:** every comment is checked against the PR diff first. If any
  is invalid, nothing is posted.
- **Existing pending review:** if the user already has one on the PR (GitHub
  allows only one per user per PR), the comments are added to it through GraphQL
  `addPullRequestReviewThread`. Otherwise a new pending review is created through
  REST without an `event`.
- **Never submits.** On their own PR the user can only submit as "Comment"; on
  someone else's PR they can choose to approve or request changes.
- **No review summary:** a review-level body isn't shown in the browser's "Finish
  your review" dialog and can't be set on a pending review. Put summaries in the
  PR description or a separate PR comment.
- **Line numbers while pending:** the API reports `line: null` for comments in a
  pending review. They land on the right lines once submitted.
- **gh binary:** uses `$GH`, else `gh` on `PATH`. Set `GH=~/miniforge3/bin/gh`
  when `gh` isn't on `PATH`.
