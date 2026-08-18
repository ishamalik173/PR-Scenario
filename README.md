# Inkwell — AI Feature Review demo

A static blog landing page, plus an AI reviewer that runs as a **merge gate**
on every pull request into `main`.

There is no local demo script. The whole scenario is the PR flow.

```
test  --(PR)-->  main
                   |
                   +-- .github/workflows/ai-feature-review.yml
                         1. python review_cli.py --mode ci  -> review-output.json
                         2. post findings as a PR comment
                         3. exit non-zero if anything blocking  -> merge blocked
```

## Layout

| Path | What it is |
|---|---|
| `site/` | the website — `index.html`, `styles.css`, `scripts.js` |
| `PLAN.md` | the build record: design, deviations, what is and isn't verified |
| `MCP_PLAN.md` | comparing MCP tool descriptions — separate from the PR flow |
| `CLAUDE.md` | the review standards: REPORT / SKIP / scope / severity |
| `.claude/settings.json` | `permissions.deny` — the secret exclusion |
| `.env` | fake credentials, committed on purpose as the deny-rule fixture |
| `review_cli.py` | the reviewer; one file, two modes |
| `refactor_tools.py`, `compare_tools.py` | the MCP comparison. Not used by the PR flow |
| `.github/workflows/ai-feature-review.yml` | the PR job and the merge gate |

## Branches

- `main` — the plain landing page. Baseline.
- `test` — the enhanced page. This is where new work lands, and where the
  planted defects live.

## Running it

Requires `ANTHROPIC_API_KEY` in the environment (in CI: a repository secret of
the same name).

```bash
pip install -r requirements.txt

# what a developer runs before opening the PR
python review_cli.py --feature landing_page --mode local

# exactly what CI runs — one flag different
python review_cli.py --feature landing_page --mode ci --out review-output.json

# print the configuration and the prompt without calling the API
python review_cli.py --mode ci --show-options
```

### Exit codes

| Code | Meaning | Merge |
|---|---|---|
| `0` | clean, or only non-blocking findings | allowed |
| `1` | `security` or `logic` finding at `high`/`critical` | **blocked** |
| `2` | the reviewer itself failed (API error, bad config) | **blocked** |

`1` and `2` are kept distinct deliberately. "The code is bad" and "the reviewer
is broken" need different responses, and collapsing them is how a team learns
to ignore a red pipeline.

## What the two modes actually differ in

`--mode` is not a second code path. It selects a bundle of configuration, and
that bundle is what enforces the behaviour.

| | `local` | `ci` |
|---|---|---|
| `setting_sources` | `["project"]` — `CLAUDE.md` found on disk | `[]` — nothing discovered |
| standards | discovered | injected into the system prompt |
| `tools` | `Read`, `Grep`, `Glob` | `Read`, `Grep`, `Glob` |
| `settings` | `.claude/settings.json` by path | `.claude/settings.json` by path |
| output | text for a human | JSON for the workflow to parse |

Two things worth saying out loud when demoing this:

**The deny rules are loaded by path, not by discovery.** CI runs with
`setting_sources=[]`, so `.claude/settings.json` is never found automatically.
`review_cli.py` passes it explicitly via `settings=`. Turning discovery off to
make startup lean is only half an answer — the other half is handing back the
context you just switched off. A `.claudeignore` file here would be inert:
nothing reads it. `permissions.deny` is the rule that actually fires, and a
blocked read shows up in `review-output.json` under `denied_calls`.

**The reviewer cannot edit anything, and not because it was asked not to.**
Neither mode puts `Edit` or `Write` in the `tools` list, so no writing tool
exists in the session at all. The instruction "report, do not modify" is
belt; the tool list is braces.

## Setting up the repo

```bash
git init
git add .
git commit -m "Initial landing page + AI review workflow"
git branch -M main
git remote add origin git@github.com:<you>/<repo>.git
git push -u origin main

git checkout -b test
# make the enhancements
git push -u origin test
```

Then open a PR from `test` into `main` and add `ANTHROPIC_API_KEY` under
**Settings → Secrets and variables → Actions**. To make the gate binding
rather than advisory, add a branch protection rule on `main` requiring the
`ai-feature-review` check to pass.
