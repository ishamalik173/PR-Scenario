# MCP_PLAN.md — comparing tool descriptions

Companion to `PLAN.md`. Covers the one piece that is not part of the PR flow:
an in-process MCP refactor server, and the comparison it exists to run.

---

## 1. The question

**Does the wording of a tool description change which tool the agent picks?**

An agent chooses a tool from its description, not its name. Two tools with the
same name, the same inputs and the same implementation should get picked at
different rates if one description says what the tool is for and the other
does not.

So: run the same refactoring job twice, against the same code, with the same
tools available, changing exactly one thing — the wording.

| Run | `extract_function` description |
|---|---|
| `vague` | `"Extracts a function from code."` — 30 characters |
| `detailed` | what it does, when to use it, when **not** to, why to prefer it over `Edit`, the parameters, a worked example — 1267 characters |

Everything else is identical: same input schemas, same Python code behind them,
same built-in tools, same model, same job.

---

## 2. Why this is separate from the PR review

The first idea was a `--with-mcp vague|detailed` flag on `review_cli.py`. That
does not work, and the reason is worth keeping.

**The reviewer is read-only.** It runs `tools=["Read", "Grep", "Glob"]` in both
modes and reviews a static landing page. It has no reason to extract a function
or rename anything, so it would never call the refactor tools *in either run*.
Both would come back with zero calls, the output would say "no difference", and
that would be a fact about the setup rather than about descriptions. Something
that cannot come out either way is not worth running.

So the MCP server is driven by `compare_tools.py`, which:

- gives the agent `Edit` as well, so ignoring the MCP tools is a real option;
- gives it a job that genuinely needs refactoring;
- leaves `review_cli.py` and the PR workflow **completely untouched**.

The read-only reviewer is a claim this repo makes elsewhere. Weakening it to
host this comparison would cost more than the comparison is worth.

---

## 3. What was built

### `refactor_tools.py`

An in-process MCP server (`create_sdk_mcp_server`), so there is no second
process to manage and the wording is just a Python variable.

Two tools, both **advisory** — they work out the change and return it as a
diff, and never write to disk:

| Tool | What it does |
|---|---|
| `extract_function(file, start_line, end_line, new_name)` | Pulls a block of JavaScript into a new top-level function, works out the parameter list from the variables the block uses but does not define, returns a diff. |
| `rename_symbol(old_name, new_name)` | Whole-name rename across every `.js`, `.css` and `.html` file under `site/`; returns the affected files with a count each. |

Advisory on purpose. The agent still has `Edit`, so what is being compared is
*choice*, not capability — and neither run can damage the other's copy through
the MCP path.

### `compare_tools.py`

```bash
python compare_tools.py                          # both, compared
python compare_tools.py --descriptions detailed  # just one
python compare_tools.py --format json            # machine-readable
python compare_tools.py --show-options           # settings + job, no API call
```

Each run gets its own throwaway copy of the source files in a temp folder, so
the two runs cannot affect each other and your real files are never touched.
`refactor_tools.REPO` points at that copy for the duration of the run, so the
MCP tools and `Edit` are looking at the same files.

`strict_mcp_config=True` is set: without it a stray project-level MCP config
could add servers and the two runs would stop being comparable.

### The job

```
Two changes to `site/scripts.js`:

1. The newsletter submit handler validates the email inline. Pull that
   validation out into its own top-level function called `isValidEmail`.
2. Rename the `status` variable to `statusEl` everywhere it appears.

Show me the resulting change.
```

It needs both tools, and both are things `Edit` could also do. That is what
makes the choice worth watching rather than forced.

The job needs `site/scripts.js`, which exists only on the `test` branch.
`compare_tools.py` checks for it and says so rather than failing obscurely.

### What gets measured

Which tools actually got called. Did `mcp__refactor__*` show up, or did the
agent reach for `Edit` and `Grep`?

```
  --- vague descriptions ---------------------------------
      tools called : Read -> Grep -> Edit -> Edit
      MCP calls    : 0
      Edit/Write   : 2

  --- detailed descriptions ------------------------------
      tools called : Read -> mcp__refactor__extract_function -> mcp__refactor__rename_symbol
      MCP calls    : 2
      Edit/Write   : 0
```

(Made up, to show the shape. See §5 — this has not been run against a funded
key.)

---

## 4. Three bugs this surfaced

All three were caught by running the tools directly before wiring a model to
them, and each would have quietly ruined the comparison.

### Parameters picked up out of string literals

The first `extract_function` produced:

```
isValidEmail(Check, Please, Thanks, address)
```

`Check`, `Please`, `Thanks` and `address` are words out of
`'Thanks! Check your inbox to confirm.'` and `'Please enter a valid email
address.'`. The scan for variable names was reading ordinary English inside
quoted strings.

Fixed by stripping strings, template literals and comments before looking for
names. Now:

```
handleEmail(email, status, subscribe)
```

Exactly the three variables the block uses but does not define, which is the
whole selling point of the tool.

### No check that the block closes its own braces

The tool would happily "extract" a half-open block — `} else {` through `});` —
and hand back JavaScript that cannot run. It now refuses a range whose braces
or brackets do not close inside it:

```
Lines 26-31 of site/scripts.js cannot be extracted: the block closes a brace it
never opened. Widen or narrow the range to a complete statement.
```

This matters beyond correctness. The detailed description claims *"PREFER THIS
OVER Edit for extraction"*. A tool that hands back broken code makes that claim
false, and an agent that tried it once and got garbage would be right to fall
back to `Edit` — which would have shown up as "no difference" and been read as
"the wording didn't matter".

---

### The rename matched inside hyphenated names

The detailed description promises:

> PREFER THIS OVER Grep-then-Edit: a manual sweep matches substrings, so
> renaming `nav` also corrupts `nav-toggle` and `site-nav`. This does not.

It did. The old pattern was `\bnav\b`, which matches inside `nav-toggle`,
because a hyphen counts as a word boundary — so the tool had exactly the flaw
it was selling itself as the cure for. CSS class names are full of hyphens,
which is the case the description leads with.

Fixed by treating `-` as part of a name: `(?<![\w-])name(?![\w-])`. The
difference is visible in the counts — renaming `status` used to report 4 hits
in `site/scripts.js` and 2 in `site/index.html`; the extras were inside
`newsletter-status`. It now reports 3 and 1.

This one is the sharpest of the three. A tool whose description makes a promise
its code does not keep is worse than a vague description, because the agent
learns to distrust it after one bad result — and that distrust would show up in
the comparison as "the wording didn't matter".

---

## 5. What is and isn't verified

**Working:**

- Both tools, run directly against the real `site/scripts.js`. `rename_symbol`
  correctly reports `site/scripts.js (3)`, `site/styles.css (1)`,
  `site/index.html (1)` for `status`, and correctly skips `site-nav` and
  `nav-toggle` when renaming `nav`. `extract_function` correctly works out
  `(email, status, subscribe)`.
- The refusal paths: non-JavaScript file, line numbers out of range, unbalanced
  block, a path outside `site/`, nothing to rename — each returns a clear error
  rather than nonsense.
- The MCP server connects in a live session:
  ```
  mcp_servers: [{'name': 'refactor', 'status': 'connected'}]
  tools: ['Edit', 'Glob', 'Grep', 'Read',
          'mcp__refactor__extract_function', 'mcp__refactor__rename_symbol']
  ```
- `--show-options` reports 30 characters against 1267 for the two runs — the
  thing being varied is real and measurable.

**Not working yet:**

The comparison has never finished a run. The available API key returns
`billing_error: Credit balance is too low`, the same blocker as the review flow
(`PLAN.md` §5). Everything up to the model call is verified; which tools get
called is not.

To finish:

```bash
git checkout test
python compare_tools.py
```

---

## 6. Reading the result honestly

One run is a story, not evidence. `compare_tools.py` prints that line every
time, and it is not decoration:

- The model may use the MCP tools in **both** runs — the tool *names* are
  fairly suggestive on their own here. That is a real outcome, and the honest
  reading is "the name carried it; the wording wasn't what decided it".
- The model may use them in **neither** run. Check the tools are reachable
  before concluding anything — a server that failed to connect looks exactly
  like one the agent ignored.
- The vague run may occasionally use them and the detailed run not. Run it
  again.

The output names which of these happened rather than announcing the expected
answer. That is the only way it counts as evidence instead of a magic trick.

---

## 7. What this changes in domain coverage

D2 was the thinnest domain in this repo — built-in tools and gating only.
It now covers both halves:

| | Before | After |
|---|---|---|
| Built-in tools, read-only gating | ✅ | ✅ |
| Custom MCP server | ❌ | ✅ `refactor_tools.py` |
| Wording of descriptions vs tool choice | ❌ | ✅ `compare_tools.py` |
| `strict_mcp_config` | ❌ | ✅ |

D1, D3, D4 and D5 are unaffected. The PR flow did not change — no commit in
this work touches `review_cli.py`, `CLAUDE.md`, `.claude/settings.json` or the
workflow.

---

## 8. How to demo it

1. **Show the two descriptions.** `python compare_tools.py --show-options`.
   30 characters against 1267. Same inputs, same code behind them, same name.
2. **Run both.** `python compare_tools.py`.
3. **Read which tools got called**, not the prose. The vague run falling back
   to `Grep` + `Edit` is the interesting part: that is what a manual rename
   looks like, and `Grep` matches parts of words, so renaming `nav` also hits
   `nav-toggle` and `site-nav`.
4. **Show that the tool is genuinely better**, not just preferred: ask for an
   unbalanced range and watch it refuse, then point out that `Edit` would have
   accepted it.
5. **Say the limit out loud.** One run each. This shows the mechanism; it does
   not measure how big the effect is.
