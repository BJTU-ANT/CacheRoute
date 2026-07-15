# Codex Branch Hygiene Workflow

This workflow keeps Codex-generated pull requests small, reviewable, and isolated from previous unmerged work.

## 1. Start from the latest main branch

Before creating a task branch, fetch the current main branch and reset the local base to the remote state:

```bash
git fetch origin main
git checkout main
git reset --hard origin/main
```

Do not start from an existing Codex branch, a local work branch, or a branch that contains changes from another issue.

## 2. Create a fresh issue branch

Create one new branch per issue from `origin/main`:

```bash
git checkout -b codex/issue-<number>-<short-topic> origin/main
```

Use a descriptive branch name that includes the issue number. Do not reuse an old branch name unless the old branch has been deleted and recreated from `origin/main`.

## 3. Keep the diff scoped to the issue

Make only the files required by the issue. Avoid drive-by cleanup, unrelated formatting, generated artifacts, dependency updates, or runtime behavior changes unless the issue explicitly asks for them.

For documentation-only issues, do not modify Scheduler routing, Proxy selection, IWS behavior, KDN behavior, KVCache behavior, Instance forwarding, or other runtime code paths.

## 4. Inspect the diff before committing

Check both the file list and the content diff:

```bash
git diff --name-only origin/main...HEAD
git diff origin/main...HEAD
```

The changed-file list should contain only files that are necessary for the issue. If unrelated files appear, remove those changes before committing.

## 5. Commit only the intended changes

Stage the expected files explicitly:

```bash
git add <expected-file-1> <expected-file-2>
git status --short
git commit -m "docs: document Codex branch hygiene workflow"
```

Do not use broad staging commands when the working tree contains unrelated changes.

## 6. Prepare the pull request description

The pull request description should make branch hygiene easy to verify. Include:

- base branch and base SHA,
- head branch and head SHA,
- changed files,
- why each changed file is necessary,
- confirmation that no runtime code was changed,
- confirmation that Scheduler routing, Proxy selection, IWS, KDN, KVCache, and Instance forwarding were not changed.

## 7. Final validation

Before opening the pull request, run:

```bash
git diff --name-only origin/main...HEAD
```

For a scoped task, the output must match the expected file list from the issue. Any extra file indicates branch contamination or unrelated changes that must be removed before the PR is opened.
