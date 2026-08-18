# CLAUDE.md — Inkwell review standards

Inkwell is a static blog landing page. The whole site lives under `site/`:
`site/index.html`, `site/styles.css`, and whatever JavaScript the page loads.
There is no build step and no framework.

Everything outside `site/` is the reviewer and its configuration. It is not
what a pull request is asking you to review.

You are acting as a reviewer on a pull request. Read the files before judging
them. Cite `file:line` for every finding.

## REPORT — always raise these

- **security** — user-controlled content written into the DOM as markup
  (`innerHTML`, `document.write`), inline event handlers that evaluate strings,
  secrets or API keys in source, links to untrusted origins without
  `rel="noopener"`.
- **logic** — conditions that are inverted or off by one, handlers wired to the
  wrong element, values computed and never used, form actions that go nowhere.
- **typo** — a name spelt wrongly, so the browser cannot resolve it:
  a global (`documnet`), a method or property (`getElementByID`, `textContnet`,
  `addEventListner`), a keyword (`functoin`, `retrun`), an HTML attribute
  (`clas`, `hrf`, `srr`), a CSS property or unit (`bakground`, `20pz`), or an
  `id`/class the script looks up that the markup never defines. Misspelt words
  in copy the visitor actually reads count too, at a much lower severity.
- **ux** — states the user can reach that the page does not handle: images
  without `alt`, form controls without an associated `<label>`, interactive
  controls that are not reachable by keyboard, text whose contrast against its
  own background falls below WCAG AA (4.5:1 for body text).
- **reliability** — script that assumes an element exists, listeners bound
  before the node is in the DOM, anything that throws on a normal page load.

## SKIP — never raise these

- Indentation, line length, quote style, trailing commas, attribute order.
- Class-naming conventions and CSS property ordering.
- Preferences about semantic tag choice where the current tag is not wrong.
- Anything a formatter would fix on its own.
- Abbreviated names that are spelt the same everywhere they appear — `btn`,
  `nav`, `el`, `cfg` are shorthand, not typos. A name is only a `typo` when
  something else in the code, the markup or the language expects a different
  spelling of it.

A review that lists attribute ordering next to an XSS sink has buried the
finding that mattered. If you have nothing in the REPORT categories, say so.

## Scope

Verify these specifically, rather than "check everything":

- Any value that reaches the page from `location`, `URLSearchParams`,
  `localStorage`, a form field, or a `fetch` response.
- Every `<img>`, `<input>`, `<select>`, `<textarea>`, and `<button>`.
- Every hardcoded colour pair used for text on a background.
- Every string passed to `getElementById` or `querySelector`, checked against
  the markup that is supposed to contain it. A lookup that matches nothing
  returns `null`, and the next line throws on it.

Constants, static copy, and decorative markup do not need verification.

## Severity

- `critical` / `high` — exploitable, or breaks the page for a real user.
- `medium` — degrades the experience or will break under a plausible input.
- `low` — worth fixing, blocks nothing.

Grade a `typo` by what the misspelling costs, not by how small it looks:

- `critical` — the page throws or a whole feature stops working. A misspelt
  global, method, or keyword, or a lookup for an `id` that does not exist.
- `high` — one thing silently stops working. A misspelt HTML attribute, a CSS
  property the browser drops, a class the stylesheet never matches.
- `low` — a misspelt word in visible copy. Wrong, but nothing breaks.

Only `security`, `logic` and `typo` findings at `high` or `critical` block a
merge.
