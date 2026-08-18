"""In-process MCP refactor server — used by `compare_tools.py`.

Two sets of descriptions, one implementation. The only difference between the
two runs is the wording in the `@tool` description, which is exactly the
question being asked: does that wording change which tool the agent picks?

Runs in-process via create_sdk_mcp_server(), so there is no second process to
manage and the set of descriptions is just a Python variable.

Both tools are ADVISORY: they work out the change, return it as a diff, and
never write to disk. The agent still has `Edit` available — that is the point.
If it reaches for `Edit` instead of these, the descriptions did not do their
job.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

# Repo root. `compare_tools.py` repoints this at a throwaway copy for the
# duration of a run, so the tools and `Edit` are looking at the same files.
ROOT = Path(__file__).resolve().parent

# The website lives here. These tools only ever touch it — the reviewer, its
# configuration and the docs are not refactoring targets.
SITE = "site"

# Files these tools understand. This is a static site, so "refactoring" means
# JavaScript functions and names shared across markup and styles.
SOURCE_GLOBS = ("*.js", "*.css", "*.html")

# --------------------------------------------------------------------------
# The two description sets. This is the whole experiment.
# --------------------------------------------------------------------------

DESCRIPTIONS_VAGUE = {
    "extract_function": "Extracts a function from code.",
    "rename_symbol": "Renames a symbol.",
}

DESCRIPTIONS_DETAILED = {
    "extract_function": (
        "Extract a contiguous block of statements out of a JavaScript file into a new "
        "top-level function, and replace the original block with a call to it. Infers "
        "the parameter list from the block's free variables and returns a unified diff. "
        "Does not write to disk.\n"
        "\n"
        "USE THIS WHEN: asked to extract, pull out, split up, or factor out part of a "
        "script; when a handler has grown long and a cohesive block should become its "
        "own unit; when the same block appears in more than one place.\n"
        "\n"
        "DO NOT USE THIS WHEN: renaming something (use rename_symbol); moving code "
        "between files; changing behaviour rather than structure; the target is HTML "
        "or CSS rather than JavaScript.\n"
        "\n"
        "PREFER THIS OVER Edit for extraction: it derives the parameter list from the "
        "block's actual free variables, so the new call site cannot silently drop a "
        "variable the way a hand-written edit can.\n"
        "\n"
        "PARAMETERS:\n"
        "  file        repo-relative path, e.g. 'site/scripts.js'\n"
        "  start_line  first line of the block to extract (1-indexed, inclusive)\n"
        "  end_line    last line of the block (1-indexed, inclusive)\n"
        "  new_name    name for the extracted function, camelCase\n"
        "\n"
        "EXAMPLE: to pull the email validation out of the submit handler at lines "
        "24-31, call with file='site/scripts.js', start_line=24, end_line=31, "
        "new_name='isValidEmail'."
    ),
    "rename_symbol": (
        "Rename an identifier — a JavaScript function or variable, or a CSS class — "
        "across every .js, .css and .html file under site/. Matches whole identifiers "
        "only, treating a hyphen as part of a name, so longer names that contain the "
        "one being renamed are left alone. Returns the affected files with a per-file "
        "match count. Does not write to disk.\n"
        "\n"
        "USE THIS WHEN: asked to rename, re-label, or consistently re-spell something "
        "that appears in more than one file; when a CSS class and the markup that "
        "uses it must stay in step.\n"
        "\n"
        "DO NOT USE THIS WHEN: changing a string literal or user-visible copy; "
        "renaming something that appears exactly once (a plain Edit is clearer); "
        "extracting or moving code.\n"
        "\n"
        "PREFER THIS OVER Grep-then-Edit: a manual sweep matches substrings, so "
        "renaming `nav` also corrupts `nav-toggle` and `site-nav`. This does not.\n"
        "\n"
        "PARAMETERS:\n"
        "  old_name    the current identifier, e.g. 'subscribe'\n"
        "  new_name    the replacement identifier\n"
        "\n"
        "EXAMPLE: to rename the `status` variable to `statusEl`, call with "
        "old_name='status', new_name='statusEl'."
    ),
}

# Identifiers that are never free variables of an extracted block.
_JS_GLOBALS = {
    "var", "let", "const", "function", "return", "if", "else", "for", "while",
    "true", "false", "null", "undefined", "new", "typeof", "this", "document",
    "window", "console", "JSON", "Math", "Object", "Array", "String", "Number",
    "fetch", "URLSearchParams", "event", "location", "localStorage", "setTimeout",
}

_DECLARED_RE = re.compile(r"\b(?:var|let|const|function)\s+([A-Za-z_$][\w$]*)")
_ASSIGNED_RE = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*=[^=]", re.MULTILINE)
_IDENT_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\b")
# Property accesses and object keys are not free variables: `a.b` and `{b: 1}`.
_MEMBER_RE = re.compile(r"\.\s*[A-Za-z_$][\w$]*|\b[A-Za-z_$][\w$]*\s*:")
# Strings and comments contain prose, not identifiers. Scrubbing them is not
# cosmetic: 'Please enter a valid email address.' otherwise contributes
# `Please`, `enter`, `valid`, `email` and `address` to the parameter list.
_NOISE_RE = re.compile(
    r"'(?:\\.|[^'\\])*'"      # single-quoted
    r'|"(?:\\.|[^"\\])*"'     # double-quoted
    r"|`(?:\\.|[^`\\])*`"     # template literal
    r"|//[^\n]*"              # line comment
    r"|/\*.*?\*/",            # block comment
    re.DOTALL,
)


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _resolve(rel: str) -> Path:
    """Resolve inside site/. A path that escapes it is a bug, not a feature."""
    site = (ROOT / SITE).resolve()
    path = (ROOT / rel).resolve()
    if site not in path.parents:
        raise ValueError(f"{rel} is outside {SITE}/, which is the only place these tools work")
    return path


def _free_variables(block: list[str]) -> list[str]:
    body = _NOISE_RE.sub(" ", "\n".join(block))
    declared = set(_DECLARED_RE.findall(body)) | set(_ASSIGNED_RE.findall(body))
    scrubbed = _MEMBER_RE.sub(" ", body)
    referenced = set(_IDENT_RE.findall(scrubbed))
    return sorted(
        name
        for name in referenced - declared - _JS_GLOBALS
        if len(name) > 1 and not name[0].isdigit()
    )[:4]


def _unbalanced(block: list[str]) -> str | None:
    """Reject a block whose braces or parens do not close within it.

    This is the concrete reason to prefer this tool over a hand-written `Edit`:
    a half-open block is the failure a manual line-range extraction makes most
    often, and it produces JavaScript that will not parse.
    """
    text = _NOISE_RE.sub(" ", "\n".join(block))
    for opener, closer, label in (("{", "}", "braces"), ("(", ")", "parens")):
        depth = 0
        for char in text:
            depth += (char == opener) - (char == closer)
            if depth < 0:
                return f"closes a {label[:-1]} it never opened"
        if depth > 0:
            return f"leaves {depth} unclosed {label}"
    return None


def _extract_function_impl(args: dict[str, Any]) -> dict[str, Any]:
    rel = args["file"]
    start, end = int(args["start_line"]), int(args["end_line"])
    new_name = args["new_name"]

    try:
        path = _resolve(rel)
    except ValueError as exc:
        return _err(str(exc))
    if not path.exists():
        return _err(f"No such file: {rel}")
    if path.suffix != ".js":
        return _err(f"{rel} is not JavaScript. This tool only extracts from .js files.")

    lines = path.read_text(encoding="utf-8").splitlines()
    if not (1 <= start <= end <= len(lines)):
        return _err(f"Line range {start}-{end} is outside {rel} (1-{len(lines)}).")

    block = lines[start - 1 : end]
    problem = _unbalanced(block)
    if problem:
        return _err(
            f"Lines {start}-{end} of {rel} cannot be extracted: the block {problem}. "
            "Widen or narrow the range to a complete statement."
        )

    indent = min((len(l) - len(l.lstrip()) for l in block if l.strip()), default=0)
    dedented = [l[indent:] if len(l) >= indent else l for l in block]

    params = _free_variables(dedented)
    signature = f"function {new_name}({', '.join(params)}) {{"
    body = "\n".join("  " + l if l.strip() else l for l in dedented)
    call = " " * indent + f"{new_name}({', '.join(params)});"

    diff = "\n".join(
        [
            f"--- a/{rel}",
            f"+++ b/{rel}",
            f"@@ -{start},{end - start + 1} +{start},1 @@",
            *[f"-{l}" for l in block],
            f"+{call}",
            "",
            "// new top-level function:",
            signature,
            body,
            "}",
        ]
    )
    return _ok(
        f"Extracted lines {start}-{end} of {rel} into `{new_name}"
        f"({', '.join(params)})`. Not written to disk.\n\n{diff}"
    )


def _rename_symbol_impl(args: dict[str, Any]) -> dict[str, Any]:
    old_name, new_name = args["old_name"], args["new_name"]
    # NOT `\b...\b`. A hyphen is a word boundary, so `\bnav\b` matches inside
    # `nav-toggle` and `site-nav` — which is exactly the failure this tool
    # promises to avoid, and CSS class names are full of hyphens. Treating `-`
    # as part of an identifier is what makes the promise true.
    pattern = re.compile(rf"(?<![\w-]){re.escape(old_name)}(?![\w-])")

    changed: list[str] = []
    for glob in SOURCE_GLOBS:
        for path in sorted((ROOT / SITE).glob(glob)):
            hits = len(pattern.findall(path.read_text(encoding="utf-8")))
            if hits:
                changed.append(f"{path.relative_to(ROOT).as_posix()} ({hits})")

    if not changed:
        return _ok(f"No whole-identifier match for `{old_name}`. Nothing to rename.")
    return _ok(
        f"`{old_name}` -> `{new_name}` would change {len(changed)} file(s), whole "
        f"identifiers only. Not written to disk.\n"
        + "\n".join(f"  - {c}" for c in changed)
    )


_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "file": {"type": "string", "description": "Repo-relative path to the .js file"},
        "start_line": {"type": "integer", "description": "First line of the block, 1-indexed"},
        "end_line": {"type": "integer", "description": "Last line of the block, 1-indexed"},
        "new_name": {"type": "string", "description": "Name for the extracted function"},
    },
    "required": ["file", "start_line", "end_line", "new_name"],
}

_RENAME_SCHEMA = {
    "type": "object",
    "properties": {
        "old_name": {"type": "string", "description": "Current identifier"},
        "new_name": {"type": "string", "description": "Replacement identifier"},
    },
    "required": ["old_name", "new_name"],
}

DESCRIPTION_SETS = {"vague": DESCRIPTIONS_VAGUE, "detailed": DESCRIPTIONS_DETAILED}


def build_refactor_server(description_set: str = "detailed"):
    """Build the in-process MCP server with the requested descriptions.

    `description_set` is "vague" or "detailed". Everything else — schemas,
    implementations, tool names — is identical between the two.
    """
    texts = DESCRIPTION_SETS.get(description_set)
    if texts is None:
        raise ValueError(
            f"unknown description set {description_set!r} "
            f"(expected one of {', '.join(sorted(DESCRIPTION_SETS))})"
        )

    @tool("extract_function", texts["extract_function"], _EXTRACT_SCHEMA)
    async def extract_function(args: dict[str, Any]) -> dict[str, Any]:
        return _extract_function_impl(args)

    @tool("rename_symbol", texts["rename_symbol"], _RENAME_SCHEMA)
    async def rename_symbol(args: dict[str, Any]) -> dict[str, Any]:
        return _rename_symbol_impl(args)

    return create_sdk_mcp_server(
        name="refactor", version="1.0.0", tools=[extract_function, rename_symbol]
    )
