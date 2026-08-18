# PLAN.md — AI Feature Review, PR-only flow

The build record for this repo: what it is, what was actually built, where the
build departed from the original design and why, and what is still unproven.

Written as-built. Where the first draft of the plan was wrong, this file says
so rather than describing a repo that does not exist.

---

## 1. What this is

A static blog landing page (`Inkwell`) with an AI reviewer wired in as a
**merge gate**. Every pull request from `test` into `main` triggers a GitHub
Actions job that reviews the changed site, posts its findings as a PR comment,
and fails the build if anything blocking turns up.

There is no local demo script and no interactive walkthrough. The PR flow *is*
the scenario. `--mode local` exists because it is the same command a developer
would run before opening the PR — it is not a second product.

```
        test  ──(PR)──►  main
                            │
                            └── .github/workflows/ai-feature-review.yml
                                  1. python review_cli.py --mode ci
                                       └─► review-output.json
                                  2. post findings as a PR comment
                                  3. exit non-zero if blocking → merge blocked
```

---

## 2. Repository layout

```
PR-Scenario/
├─ site/                             THE WEBSITE — all of it, nothing else
│  ├─ index.html
│  ├─ styles.css
│  └─ scripts.js                     (test branch only)
├─ CLAUDE.md                         review standards: REPORT / SKIP / scope / severity
├─ PLAN.md                           this file
├─ MCP_PLAN.md                       comparing MCP tool descriptions
├─ README.md                         how to run it
├─ requirements.txt
├─ .env                              fake credentials — committed on purpose
├─ .gitignore
├─ review_cli.py                     the reviewer; one file, two modes
├─ refactor_tools.py                 in-process MCP server (comparison only)
├─ compare_tools.py                  the description comparison (not in the PR flow)
├─ .claude/
│  └─ settings.json                  permissions.deny — the secret exclusion
└─ .github/
   └─ workflows/
      └─ ai-feature-review.yml       the PR job and the merge gate
```

### Branches

| Branch | Contents |
|---|---|
| `main` | plain landing page in `site/` + the full review apparatus. The baseline. |
| `test` | enhanced page: featured post, newsletter, mobile menu, `site/scripts.js`. Carries the planted defects. |

`test` is one commit ahead of `main`. Everything under `.claude/`, `.github/`
and `review_cli.py` is identical on both, so the reviewer that judges the PR is
the reviewer `main` already trusted.

### Commit history

```
* Improve landing page: featured post, newsletter, mobile menu   (test)
* Report the real reason a review failed, not the transport subtype
* Initial landing page + AI feature review workflow              (main)
```

---

## 3. The reviewer — `review_cli.py`

One file, no package. It is what a developer runs, and what CI runs, with one
flag between them.

```bash
python review_cli.py --feature landing_page --mode local                       # developer
python review_cli.py --feature landing_page --mode ci --out review-output.json # CI
python review_cli.py --mode ci --show-options                                  # config + prompt, no API call
```

### `--mode` is configuration, not a code path

`ReviewConfig.for_mode()` returns a bundle of settings. There is no `if ci:`
anywhere in the review logic — the bundle is what enforces the behaviour.

| | `local` | `ci` |
|---|---|---|
| `setting_sources` | `["project"]` — `CLAUDE.md` discovered on disk | `[]` — nothing discovered |
| standards | discovered | injected into the system prompt |
| `tools` | `Read`, `Grep`, `Glob` | `Read`, `Grep`, `Glob` |
| `settings` | `.claude/settings.json` by explicit path | `.claude/settings.json` by explicit path |
| output | text, for a human | JSON, for the workflow to parse |

### Structured output

CI does not parse prose. The reviewer runs with an `output_format` json_schema
that forces every finding into `{file, line, category, severity, summary}`,
with `category` and `severity` constrained to enums. The workflow reads
`review-output.json` and never has to guess.

### The merge gate

A finding blocks only if **both** halves hold:
`category ∈ {security, logic, typo}` **and** `severity ∈ {high, critical}`.
A `ux` issue at `critical` is loud and belongs in the comment, but it is not by
itself a reason to refuse a merge.

`typo` was added late, and the reasoning is in §4.4. There are two gates now:
`node --check` parses the script before anything else runs, and the `typo`
category covers what a parser cannot see.

| Exit | Meaning | Merge |
|---|---|---|
| `0` | clean, or only non-blocking findings | allowed |
| `1` | blocking findings | **blocked** |
| `2` | the reviewer itself failed (API error, bad config) | **blocked** |

`1` and `2` are kept distinct on purpose. "The code is bad" and "the reviewer
is broken" need different responses, and collapsing them is how a team learns
to ignore a red pipeline.

The `blocking` list is computed inside the CLI and written into the JSON, so
the exit code and the PR comment are reading the same array. They cannot
disagree.

---

## 4. Deviations from the original plan

Four things changed during the build. Each is a correction, not a shortcut.

### 4.1 The deny rules load by path, not by discovery

**The plan's config would have enforced nothing in CI.**

`.claude/settings.json` is normally found by *discovery*. CI runs with
`setting_sources=[]` — discovery off, for a lean startup. So the deny rules
would never have been loaded on the exact runs that matter, and the secret
exclusion would have quietly worked only on a developer's laptop. The demo
would still have *looked* right: no error, no warning, just no enforcement.

The fix: `review_cli.py` passes the file explicitly.

```python
settings=str(REPO / ".claude" / "settings.json")
```

This is a sharper version of the lean-CI point than the original plan made.
Turning discovery off is only half an answer. The other half is handing back
the context you just switched off — the standards *and* the permission rules.

### 4.2 The gate needs category and severity together

The plan's workflow snippet filtered findings in JavaScript, inside the
`github-script` step. That put the blocking rule in the workflow while the exit
code was decided in Python — two implementations of one policy, free to drift.

The gate now lives in `ReviewRun.blocking`, and the workflow renders what the
CLI already decided.

### 4.3 The planted defects are not documented in this repo

The original plan listed the planted issues in `PLAN.md`. That file sits in the
repo the reviewer is reading, and the reviewer has `Grep` and `Glob`. Handing
it the answer key makes every subsequent finding unfalsifiable.

The defect list is kept outside the repository. This file describes the
*categories* the `test` branch exercises — security, logic, reliability, and
several UX and accessibility gaps — and nothing more specific.

### 4.4 Typos are two problems, not one, and get two gates

The first three demo runs never mentioned a typo, for two separate reasons:
`CLAUDE.md` never asked for one, and even a `reliability`/`critical` finding
does not satisfy the blocking rule. Run 3 proved the second half — a `critical`
finding sat in the comment while the merge stayed unlocked.

The obvious fix is a linter, and for part of the problem it is the right one.
But "typo" turns out to name three different defects, and no single tool covers
them:

| | Example | `node --check` | Model |
|---|---|---|---|
| Syntax broken | `functoin f() {}`, unclosed brace | ✅ | ✅ |
| Name does not resolve | `documnet.getElementById` | ❌ valid JS | ✅ |
| Wrong member on a real object | `document.getElementByID` | ❌ valid JS | ✅ |
| `id` in JS absent from the markup | `getElementById('welcome-bannner')` | ❌ single-file | ✅ |

Rows 2–4 all parse cleanly. Row 4 is not even a single-file question: it is
`site/scripts.js` disagreeing with `site/index.html`, which is why the reviewer
reads whole files rather than a diff.

So both gates exist:

- **`node --check site/scripts.js`**, first in the workflow. Free, instant,
  identical on every run. It is `continue-on-error: true` — not because a
  parse failure is forgivable, but so the review still runs on the same file
  and the final step can enforce both. Deferred, not forgiven.
- **A `typo` category** in `FINDINGS_SCHEMA`, in `BLOCKING_CATEGORIES`, and
  defined in `CLAUDE.md` with its own severity table.

The severity table is the part that does the work. Without it the model grades
inconsistently and the gate becomes a coin flip; with it a misspelt global is
`critical`, a dropped CSS property is `high`, and a misspelt word in visible
copy is `low` and correctly blocks nothing.

`CLAUDE.md`'s SKIP list also gained a boundary, or the new category would eat
it: `btn`, `nav`, `el` are abbreviations. A name is a `typo` only when
something else — the markup, another file, or the language — expects a
different spelling of it.

**Honest limit:** the parser gives the same answer every time; the model does
not. Row 1 is guaranteed. Rows 2–4 are very likely and not certain. Anything
that must never slip through belongs in the parser column.

---

## 5. Verification status

Being explicit about what has and has not been proven.

### Confirmed working

- `--show-options` renders correctly in both modes, on both branches, and the
  feature file list correctly picks up `scripts.js` only where it exists.
- A real SDK session initializes with exactly the intended configuration. From
  the `init` message:
  ```
  tools:          ['Glob', 'Grep', 'Read', 'StructuredOutput']
  permissionMode: 'dontAsk'
  cwd:            .../PR-Scenario
  ```
  No `Edit`. No `Write`. No `Bash`. The tool gating is real, not aspirational.
- The `settings=` path is accepted by the transport.
- Exit codes: `2` observed and correct on a reviewer failure.

### Not yet proven

**The findings path has never run end to end.** The API key available during
the build (`Week4_agent_sdk/.env`) returns:

```
billing_error: Credit balance is too low
```

So the reviewer starts, configures itself correctly, and then cannot call the
model. Everything downstream of the model call — actual findings, the exit-1
gate, the PR comment body, the `denied_calls` array — is written and wired but
unexercised.

To finish verifying, with a funded key:

```bash
git checkout test
python review_cli.py --feature landing_page --mode ci --format text
# expect: >= 2 blocking findings, exit code 1
```

### A bug this surfaced

The first failing run reported:

```
REVIEW FAILED: Exception: Claude Code returned an error result: success
```

Useless in a CI log. The transport raises with the `ResultMessage` subtype,
which for a billing or auth failure is literally the word `success`; the real
reason arrives one message earlier, on the assistant turn, as
`AssistantMessage.error`. `run_review` now captures that and reports
`billing_error: Credit balance is too low`.

Worth keeping in mind generally: an exit-2 path that cannot say *why* is an
exit-2 path a team will start ignoring.

---

## 6. Domain coverage

What to say out loud when demoing this, per domain.

### D3 — Configuration & Workflows (primary, ~7–9 questions)

- **CLAUDE.md as the source of truth.** `CLAUDE.md` carries REPORT / SKIP /
  scope / severity. In `local` it is discovered; in `ci` it is read off disk
  and injected into the system prompt. Same standards, two delivery routes.
- **Secret exclusion, enforced.** `.claude/settings.json` denies `Read(./.env)`.
  A `.claudeignore` here would be an inert artifact — nothing reads it.
  `permissions.deny` is the rule that fires, and a blocked read lands in
  `review-output.json` under `denied_calls` and in the PR comment. Evidence,
  not trust.
- **Lean CI startup, and its trap.** `setting_sources=[]` is the fast path, and
  §4.1 is the reason it is dangerous on its own.
- **Tool gating.** Neither mode puts `Edit` or `Write` in `tools`. The prompt
  says "do not modify any files"; the tool list is why it cannot. Instruction is
  belt, configuration is braces — and the `init` message in §5 is the receipt.
- **Skills vs commands.** Not built here (see §7). Explain conceptually: a
  command is user-invoked, a skill is model-invoked.

### D5 — Context Management & Reliability (~2–3 questions)

- **Fresh session per PR.** Every workflow run is a new process and a new
  session. Nothing carries over from the run that reviewed the previous commit,
  so there is no prior reasoning to be anchored by. Contrast with a developer
  who refactors and self-reviews in one long session — that reviewer has
  already convinced itself.
- **Repeatable by construction.** Same config, same standards, same code →
  the same review. The variance that remains is the model's, not the harness's.

### D2 — Tool Design (~2–3 questions)

- **Dedicated tools over shell.** `Read`, `Grep`, `Glob` — not `Bash` with
  `cat` and `grep`. Structured inputs, structured results, and each call is
  individually gateable.
- **The tool set is the boundary.** Read-only in both modes. The agent reports;
  it cannot fix.
- **Tool descriptions drive selection.** A custom MCP server with two
  sets of descriptions — 30 characters against 1267, everything else
  identical. Separate entry point (`compare_tools.py`); the PR flow does not use it. Full
  design, bugs and caveats in `MCP_PLAN.md`.

### D4 — Prompt Engineering & Structured Output (~1–2 questions)

- **Explicit REPORT / SKIP.** Formatting, naming, and quote style are named as
  SKIP. A review that lists attribute ordering next to an XSS sink has buried
  the finding that mattered.
- **Scoped verification.** `CLAUDE.md` names *what* to verify — values from
  `location`, `URLSearchParams`, `localStorage`, form fields, `fetch`
  responses — rather than "always verify data sources", which would apply to
  every constant on the page.
- **Schema, not prose.** §3 covers this; the schema is what makes the gate
  mechanical.

### D1 — Agentic Architecture (~0–1 question)

- **Autonomous, in CI.** PR opened → review runs → structured verdict →
  merge decision. No human in the loop during the review itself.
- **Conversational vs autonomous.** The local mode is where a conversational
  flow would live. CI is the fully autonomous end of the same tool.

---

## 7. Not built (deliberate)

This repo commits to the PR-only flow as the whole of Scenario 2. Several
topics from the broader Scenario 2 discussion are therefore **not artifacts
here**. Some are covered in narration; some are simply absent. Listed so the
omissions read as choices rather than gaps — and so nobody demoing this claims
coverage the repo does not have.

### ~~MCP refactor server~~ — now built, see `MCP_PLAN.md`

An in-process MCP server (`refactor_tools.py`) with `extract_function` and
`rename_symbol`, plus the vague-vs-detailed description comparison
(`compare_tools.py`). It is a **separate entry point**, not part of the PR
flow: the reviewer is read-only and would never call a refactor tool, so both
runs would record zero calls and the comparison could not come out either way.
`MCP_PLAN.md` has the
design, the two bugs it surfaced, and its own verification status.

Nothing in the PR flow changed to accommodate it.

### Skills vs commands

No `/review-feature` command file and no `feature-review` `SKILL.md` with
trigger or description variants. The distinction — command is user-invoked,
skill is model-invoked — is **explained in words only**, not demonstrated by
working artifacts. Two files if it ever needs to be shown rather than said.

### Scratchpad / context handoff

No `feature_findings.md` or equivalent passing context between sessions. The
D5 "fresh session per PR" point is made without any explicit handoff
mechanism, which is honest: fresh sessions are the claim, and a scratchpad
would be the *other* half of D5 rather than support for this half.

### Local interactive demo flow

No separate demo script and no guided walkthrough. `--mode local` is a real
configuration, but there is no class-demo flow beyond running the CLI by hand.

### Extended thinking and split-up review prompts

No experiments comparing zero-shot vs chain-of-thought vs extended-thinking
prompts, and none splitting a single multi-aspect review into separate
security and business-logic passes. Discussed in the wider Scenario 2 notes;
not built here.

### Plan mode vs direct execution

No paired tasks showing a simple fix by direct execution against a complex
refactor via plan mode. Explainable in words, but nothing in this repo shows it.

### A test suite for the site

No JavaScript or HTML tests, and the workflow runs the review directly rather
than gating on tests first. When there is something worth testing, the tests
should run *before* the review — a review of code that fails its own tests
wastes a model call and buries the real signal under noise.

### What this means for coverage

Section 6 says what each domain *is* covered by. This is the other side of it.

| Domain | Covered by | Not covered |
|---|---|---|
| **D3** Config & Workflows | `CLAUDE.md`, `setting_sources`, injected standards, `permissions.deny`, lean CI config, tool gating | skills and commands as working artifacts |
| **D5** Context & Reliability | fresh session per PR, repeatable configuration | scratchpad handoff (conceptual only) |
| **D2** Tool Design | built-in tools, read-only gating, MCP server, description comparison (`MCP_PLAN.md`) | — |
| **D4** Prompt Engineering | REPORT / SKIP, scoped verification, structured JSON | extended thinking, multi-aspect prompt splitting |
| **D1** Agentic Architecture | autonomous CI review | conversational-vs-autonomous beyond the local/CI modes |

Every one of these can be layered on later without touching the core PR flow.
That is the point of keeping `--mode` a configuration bundle rather than a
branch in the code.

---

## 8. Setup checklist

- [x] Repo initialized, `main` and `test` branches created
- [x] Landing page on `main`; enhanced page + defects on `test`
- [x] `CLAUDE.md`, `.claude/settings.json`, `.env` fixture
- [x] `review_cli.py` — two modes, structured output, three exit codes
- [x] `.github/workflows/ai-feature-review.yml` — comment + gate + artifact
- [x] Configuration verified against a live session (§5)
- [ ] Findings path verified end to end — **blocked on a funded API key**
- [ ] `git remote add origin …` and push both branches
- [ ] `ANTHROPIC_API_KEY` added under Settings → Secrets and variables → Actions
- [ ] Branch protection on `main` requiring the `ai-feature-review` check

Until that last box is ticked the gate is advisory: the job goes red, but
nothing stops the merge button.

---

## 9. The demo, in five beats

1. **Show the baseline.** `main`, plain page, green.
2. **Show the work.** `test` — featured post, newsletter, mobile menu. It looks
   like a normal, reasonable PR.
3. **Open the PR.** Workflow fires. Comment appears with the findings table.
   Blocking issues called out at the top. Job red, merge blocked.
4. **Show why it is real, not advice.** `--show-options`: no `Edit`, no `Write`.
   Then the denied `.env` read in `denied_calls`. The agent was not asked
   nicely — it was not given the option.
5. **Fix and re-run.** Push to `test`, workflow re-runs on the same fresh-session
   terms, gate goes green, merge allowed.
