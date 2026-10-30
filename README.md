# agent-skills
A collection of agent skills I use.

## Skills

These skills are personal, this means they can contain things that are tuned to the particular setup of my machine, how I like to organise things or are just plain weird to you. Tweak them so they are ergonomic for you. How a tool feels is as important as what it does.


### [predict-cuml-nightly](predict-cuml-nightly/)

Predicts which cuML nightly CI jobs (build and test) will fail before the nightly runs, using only what was known beforehand: last nightly's failures and upstream changes since. Diagnoses each predicted failure and finds or drafts an issue for it.

### [pr-impact-brief](pr-impact-brief/)

Builds a source-linked evidence brief on whether a scikit-learn PR is worth carrying, one PR at a time. It ends with the questions the decision hinges on, never a verdict, and never writes to GitHub.

### [pr-pending-review](pr-pending-review/)

Posts inline comments you already have onto a GitHub PR as a pending (draft) review in your name, so you can edit and submit them in the browser.

### [pr-walkthrough](pr-walkthrough/)

Writes a walkthrough of a PR in the current repo, with changes ordered by causal dependency so the diff reads in a sensible order.
