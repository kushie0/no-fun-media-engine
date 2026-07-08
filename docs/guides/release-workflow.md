# Release workflow — private granular history, public milestone releases

How this project's two repos relate, and the exact procedure for rolling new work
out to the public repo as an elegant, feature-milestone commit series.

## The model

| Remote | Repo | Holds | Style |
|---|---|---|---|
| `private` | `kushie0/no-fun-media-engine-private` | **All** branches: granular `dev`, in-flight feature branches | Real history — every commit, every fixup. Prod deploys from here. |
| `origin` | `kushie0/no-fun-media-engine` (public) | `main` only | **Milestone releases** — one commit per shipped feature arc, release-notes body. Not a dev log. |

- **Daily work:** commit granular to `dev`, push (its upstream is `private/dev`).
  **Never push `dev` or feature branches to `origin`.**
- **Prod:** its `origin` is the private repo over SSH; deploy = `git pull origin dev`
  on prod (see `ssh-workflow.md`). Public releases never touch prod.
- The public series **does not need to mirror dev commits**. Each public commit is a
  *feature milestone* — "what shipped", not "how it was built". Accuracy contract:
  only that the **final tree equals dev's tree** (verified below); the intermediate
  groupings are editorial.

## Cutting a release ("version drop")

Do this whenever a feature arc (or a batch of them) is done and worth showing.

### 1. Decide the milestones

List `git log --oneline --first-parent --reverse origin/main..dev`. Group the commits
into feature arcs — usually 1–3 per release. Pick one **boundary commit** per arc:
the last dev commit belonging to that arc (arcs must be contiguous ranges along
first-parent history; minor unrelated commits inside a range just ride along —
that's fine, the milestone message describes the headline).

The **last boundary is always the dev tip**, so the release ends exactly at current code.

### 2. Write the milestone messages

One per boundary. Subject = the feature ("NAS-as-primary storage with D: fallback"),
body = short release notes: what it does, why it exists, key mechanisms. Bullets
welcome. This is the public changelog — write it for an outside reader.

### 3. Build the series (snapshot method — no merges, no conflicts)

```bash
git switch -c publish origin/main
# for each boundary, in order:
git read-tree --reset -u <boundary>       # stage that commit's exact tree
git commit -F <milestone-msg-file>        # or -C <boundary> to reuse a message verbatim
```

`read-tree` sets the tree to the boundary snapshot, so each milestone commit's diff
is "everything since the previous milestone" and the mechanics can never conflict.
Optionally set `GIT_AUTHOR_DATE="$(git log -1 --format=%aD <boundary>)"` per commit
so the public timeline matches when the work actually happened.

### 4. Gate (non-negotiable)

```bash
git diff publish dev        # MUST be empty — public code == dev code
uv run pytest               # MUST be green on the publish tip
```

### 5. Push

```bash
git push origin publish:main          # normal case: fast-forward append
git switch dev && git branch -D publish
```

Appending milestones on top of the existing public main is the default and needs no
force. Re-*presenting* already-published history (rewriting the series itself) is the
exception — it needs `git push --force-with-lease origin publish:main` and should only
happen while the public repo has no meaningful consumers. Prefer append; rewrite at
most rarely and deliberately.

After pushing, mirror the new main to private: `git push private main:main --force-with-lease`
(private/main is just a mirror of the public series).

### 6. Record it

Note the release in the shipped effort's archive bundle (or a line in the relevant
`docs/active/` doc): date, range published (`<old-main>..<new-main>`), milestone list.

## Checklist (copy per release)

- [ ] dev committed + pushed to private; tests green on dev
- [ ] Milestone boundaries picked (last one = dev tip)
- [ ] Milestone messages written (subject + release-notes body)
- [ ] `publish` built via read-tree snapshots
- [ ] `git diff publish dev` empty
- [ ] `pytest` green on publish tip
- [ ] Pushed to public main (fast-forward unless deliberately re-presenting)
- [ ] `private/main` mirror updated; local `publish` deleted
- [ ] Release noted in docs

## History of releases

- **2026-07-08** — initial split: private repo created, prod switched to it, public
  main rebuilt `644ef06..` as feature milestones (reworked same-day from an earlier
  64-commit faithful series into ~14 feature arcs). Full story:
  `docs/active/archive/2026-07-08_repo-split-publish.md`.
