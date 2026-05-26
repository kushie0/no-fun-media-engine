# Commit workflow

This guide describes how commits are made in this project — the granularity, the
message format, and how Claude announces and explains each action before executing it.

---

## Core principle

A commit is a unit of intent, not a unit of work.

- A unit of **work** is "I typed for two hours."
- A unit of **intent** is "This makes the WAV batching noise quiet."

Every commit should be independently revertable and independently understandable.

---

## During development — commit freely on the branch

While working on a feature or fix, commit frequently without worrying about polish:

```
wip: trying new suffix map approach
wip: broke something, investigating
wip: works but messy
```

These are checkpoints, not history. They let you roll back mid-investigation without
losing state. Use `wip:` prefix so they're visually distinct from intentional commits.

Never let `wip:` commits reach `main` or `dev`.

---

## At merge time — squash to intentional commits

Before merging a branch, `git rebase -i` to collapse the `wip:` commits into one
(or a small number of) intentional commits.

**Rule of thumb:** if you can write a single sentence subject that covers everything in
the branch, it's one commit. If two things happened that are independently meaningful
(e.g., a bug fix discovered while building a feature), split them.

Don't mix a bug fix with a feature in the same commit. If you discover a bug while
building something, commit the fix first on its own, then continue the feature.

---

## Commit message format

```
subject line — what changed (imperative mood, ≤72 chars)

One or more paragraphs explaining:
- WHY this change was made (the constraint, the bug, the incident)
- HOW the fix works (the mechanism, not just "changed X to Y")
- Any non-obvious invariants or side effects

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
```

**Division of labour:**
- **You write the subject line.** You know the intent better than Claude.
- **Claude drafts the body.** Claude has the full diff in context and can articulate
  the mechanism.

If you don't have a subject line ready, Claude will ask for one before committing.

---

## Claude's announcement protocol

**Before any commit**, Claude will state:

1. The proposed subject line (if you haven't provided one, Claude will ask)
2. The body it will write
3. Which files will be staged

Format:
> Ready to commit. Subject: `"your line here"`. Body will cover: [one sentence
> summary of what the body will say]. Staging: `file_a.py`, `file_b.py`.
> Confirm?

**Before any `git rebase -i`**, Claude will state:

1. Which commits are being touched
2. What operation (squash, reword, split)
3. The resulting commit structure

Format:
> About to rebase. Will squash commits X, Y, Z into one commit with subject
> `"proposed subject"`. The other N commits are untouched. Confirm?

**Claude never runs `git commit`, `git rebase`, or `git push --force` without
announcing the plan first in that same turn.**

---

## Rapid development with multiple things in flight

When you're testing several fixes at the same time:

1. **One branch per thing**, even small things. `git worktree add` or `git stash`
   to switch contexts cleanly.

2. **Fix it, then feature it.** If you find a bug while building a feature, stop,
   commit the fix on `main`/`dev`, merge that into your feature branch, then continue.
   This makes the fix independently revertable.

3. **The subject line is the gate.** Before committing, ask: can I write a single
   sentence that covers everything in this diff? If not, the diff mixes concerns —
   split it first.

---

## What a clean public history looks like

A 20-commit log of intentional, single-purpose commits reads better to an outside
developer than 4 mega-commits with 3000-line diffs. Mega-commits signal unclear
scoping; atomic commits signal craft.

Good:
```
making engine launch more friendly to SSH
sharepoint cleanup and fixes
TUI log and status cleanup
initial release
```

Bad (each is a red flag):
```
fix stuff
wip
more fixes
oops
```

Also bad (the mega-commit pattern):
```
[2847 lines changed] add TUI visibility, cloud filename strip, sharepoint fix, clip ETA
```

The goal is commits that a future developer (or future you, debugging a regression)
can bisect, read, and revert independently.
