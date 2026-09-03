# Release checklist

Every gate below has a command or a URL you can run. That is the point of the
list: a checklist of intentions is a list of things somebody will assume were
done. If a gate has no observable check, it does not belong here.

Run them in order. Anything that fails stops the release.

## 1. The tree and the branch

| Gate | Check | Pass means |
| --- | --- | --- |
| Nothing uncommitted | `git status --porcelain` | no output |
| You are releasing from `development` | `git branch --show-current` | `development` |
| `master` is the only publication target | `git log --oneline -1 master` | the last release point, not a hand commit |
| No dev-only docs are about to be published | `git ls-tree -r --name-only master \| grep -E 'README-Development\|ROADMAP-Development\|125.md\|docs/report/'` | no output |
| No database is tracked on the release branch | `git ls-tree -r --name-only master \| grep '^databases/'` | no output (`tests/databases.test.js` asserts it too) |
| Every database path is ignored | `node --test tests/databases.test.js` | pass |

## 2. Private data

The hygiene gate scans **history**, not the current tree, because deleting a
file in a later commit leaves the blob reachable from the branch.

```sh
LODESTAR_PRIVATE_NAMES='<the names, comma-separated>' \
  node scripts/release-hygiene.mjs master $(git tag --list 'v*' --merged master)
```

Pass means exit 0. Exit 2 means you supplied no names — that is a refusal, not
a skip, and `npm run hygiene` is the same command with the tag list filled in.

## 3. The tests

| Suite | Command | Pass means |
| --- | --- | --- |
| Server units | `node --test tests/*.test.js` | pass — Postgres cases skip with no shared server |
| Brain units (offline) | `uv run --project brain pytest brain/tests -v` | pass, with no extras installed |
| End to end | `uv run --with playwright python tests/e2e_test.py` | pass |
| Everything, with a backup first | `npm run test:all` | pass |

## 4. Media and links

| Gate | Check | Pass means |
| --- | --- | --- |
| Every gallery asset is tracked, not ignored | `git check-ignore docs/img/*` | no output |
| Every README image resolves from a clean checkout | `grep -o 'docs/img/[A-Za-z0-9._-]*' README.md \| sort -u \| xargs -I{} git ls-files --error-unmatch {}` | no error |
| No asset carries private data | open each file in `docs/img/` and look | seed or fixture data only |
| No claim contradicts the code | `node --test tests/docclaims.test.js` | pass |

## 5. The release point

```sh
node scripts/release-to-master.mjs --check    # report and stop
npm run release                               # build the release point
```

Exit 2 from `--check` means `development` has gained nothing presentable — no
`feat`/`fix`/`perf`/`refactor`, or no file changes. Refusing beats minting a
version nobody can describe.

## 6. The push, and only then the badges

```sh
git push personal master
git push personal --tags   # v* only; never --all, never development
```

`git push --all` always fails while `development` exists — the `pre-push` hook
refuses that branch, and a refused ref aborts the whole push. Publish by naming
`master`.

| Gate | Check | Pass means |
| --- | --- | --- |
| CI ran on the pushed ref | the Actions tab for the pushed commit | one run, all four steps green |
| Server, brain and e2e all executed | the run's step list | none skipped or missing |
| Badges point at the real workflow | click each badge in the rendered README | each resolves to this repository's run |

**Badges go in after the green run, never before.** A badge added in advance is
a promise; added afterwards it is a status signal. That ordering is the whole
reason step 6 comes last.

## 7. The rendered page

Open the repository's public URL and read the README as a stranger would.

| Gate | Pass means |
| --- | --- |
| Every image renders | no broken-image icons |
| Every link resolves | including the ones into `docs/decisions/` |
| The licence wording matches `LICENSE` | both say all rights reserved, both grant the same local-use permission |
| Nothing private is visible | no real names, no real card text, no chat content |
