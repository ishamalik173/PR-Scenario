"""AI Feature Review — the command CI runs on every PR.

    python review_cli.py --feature landing_page --mode ci --out review-output.json

`--mode` is the only thing that changes between a developer's local run and the
CI run. It is not a second code path: it selects a bundle of configuration
(discovery, tools, output format), and that bundle is what enforces behaviour.

    local : CLAUDE.md discovered from the repo, read-only tools, text output
    ci    : no discovery at all, standards injected into the system prompt,
            read-only tools, JSON on stdout, exit code as the merge gate

Exit codes — CI depends on these being distinct:

    0  clean, or only non-blocking findings
    1  blocking findings (security/logic/typo at high or critical) -> block the merge
    2  the reviewer itself failed (API error, bad config) -> not a verdict
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent

# Load the API key BEFORE importing the SDK. load_dotenv does not overwrite an
# already-set variable, so a real environment variable wins — which is how CI
# supplies it, from the ANTHROPIC_API_KEY repository secret.
#
# `.env.local` is gitignored and is where a developer's own key goes. The
# committed `.env` is deliberately NOT loaded: it holds fake values and exists
# only as the fixture the deny rule blocks.
try:
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env.local")
except ImportError:  # pragma: no cover — only when the extra is not installed
    pass

from claude_agent_sdk import (  # noqa: E402  (after load_dotenv, on purpose)
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    # Windows consoles default to cp1252 and crash on box-drawing characters.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_MODEL = os.environ.get("REVIEW_MODEL", "haiku")
DEFAULT_BUDGET_USD = 0.75

# --------------------------------------------------------------------------
# What "a feature" means in file terms.
# --------------------------------------------------------------------------

# The website lives under site/. Everything else in the repo is the reviewer
# and its configuration, and is not what a PR is asking to have reviewed.
SITE = "site"

FEATURES: dict[str, list[str]] = {
    "landing_page": [f"{SITE}/index.html", f"{SITE}/styles.css", f"{SITE}/scripts.js"],
}

# --------------------------------------------------------------------------
# Structured output (D4). Forcing a schema is what makes findings countable
# instead of prose the workflow has to parse out of a paragraph.
# --------------------------------------------------------------------------

FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "category": {
                        "type": "string",
                        "enum": ["security", "logic", "typo", "ux", "reliability"],
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                    },
                    "summary": {"type": "string"},
                },
                "required": ["file", "line", "category", "severity", "summary"],
            },
        }
    },
    "required": ["findings"],
}

# The merge gate. Both halves must hold: a `ux` finding at `critical` is loud,
# but it is not a reason to refuse a merge automatically.
#
# `typo` is in the blocking set because a name the browser cannot resolve is
# not a spelling preference — `documnet.getElementById(...)` throws, and
# `getElementById('welcome-bannner')` silently returns null and takes the next
# line down with it. Severity still has to hold, which is what keeps a
# misspelt word in visible copy (graded `low`) out of the gate.
BLOCKING_CATEGORIES = {"security", "logic", "typo"}
BLOCKING_SEVERITIES = {"high", "critical"}

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# A permissions.deny block arrives only as a ToolResultBlock(is_error=True) and
# never reaches ResultMessage.permission_denials, so both sources are read below.
_DENIAL_RE = re.compile(r"denied|permission|not allowed|blocked by|requires approval", re.I)

SETTINGS_FILE = REPO / ".claude" / "settings.json"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass
class ReviewConfig:
    feature: str = "landing_page"
    mode: str = "ci"
    repo: Path = REPO

    # D3 — context discovery.
    #   None        -> CLI defaults, everything discovered
    #   ["project"] -> project settings + CLAUDE.md
    #   []          -> nothing discovered at all
    setting_sources: list[str] | None = field(default_factory=lambda: ["project"])
    inject_standards: bool = False

    # D2 — tools. `tools` is what EXISTS; `allowed_tools` is what runs without
    # prompting. Neither list contains a writer, in either mode.
    tools: list[str] = field(default_factory=lambda: ["Read", "Grep", "Glob"])

    model: str = DEFAULT_MODEL
    max_budget_usd: float | None = DEFAULT_BUDGET_USD

    @classmethod
    def for_mode(cls, mode: str, feature: str = "landing_page") -> "ReviewConfig":
        if mode == "local":
            return cls(
                feature=feature,
                mode="local",
                setting_sources=["project"],  # CLAUDE.md is found on disk
                inject_standards=False,
            )
        if mode == "ci":
            return cls(
                feature=feature,
                mode="ci",
                setting_sources=[],  # lean startup: discover nothing...
                inject_standards=True,  # ...and hand the standards back explicitly
            )
        raise ValueError(f"unknown mode {mode!r} (expected 'local' or 'ci')")

    def feature_files(self) -> list[str]:
        spec = FEATURES.get(self.feature)
        if spec is None:
            raise ValueError(
                f"unknown feature {self.feature!r}. Known: {', '.join(sorted(FEATURES))}"
            )
        # scripts.js only exists on branches that added it.
        return [p for p in spec if (self.repo / p).exists()]


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass
class ToolEvent:
    name: str
    tool_use_id: str
    input_summary: str
    allowed: bool = True
    denied_reason: str | None = None
    is_error: bool = False


@dataclass
class ReviewRun:
    config: ReviewConfig
    findings: list[dict[str, Any]] = field(default_factory=list)
    events: list[ToolEvent] = field(default_factory=list)
    text: str = ""
    session_id: str | None = None
    turns: int = 0
    cost_usd: float = 0.0
    wall_clock_s: float = 0.0
    is_error: bool = False
    error: str | None = None

    @property
    def denied(self) -> list[ToolEvent]:
        return [e for e in self.events if not e.allowed]

    @property
    def tools_used(self) -> list[str]:
        seen: list[str] = []
        for event in self.events:
            if event.name not in seen:
                seen.append(event.name)
        return seen

    @property
    def blocking(self) -> list[dict[str, Any]]:
        return [
            f
            for f in self.findings
            if str(f.get("category", "")).lower() in BLOCKING_CATEGORIES
            and str(f.get("severity", "")).lower() in BLOCKING_SEVERITIES
        ]

    def exit_code(self) -> int:
        if self.is_error:
            return 2
        return 1 if self.blocking else 0


# --------------------------------------------------------------------------
# Prompt and options
# --------------------------------------------------------------------------


def build_prompt(cfg: ReviewConfig) -> str:
    listing = "\n".join(f"  - {p}" for p in cfg.feature_files())
    return "\n".join(
        [
            f"Review the `{cfg.feature}` feature of this static site.",
            "",
            "It spans these files:",
            listing,
            "",
            "Read each one before judging it. A defect in the markup, the "
            "stylesheet or the script is a defect in the feature.",
            "",
            "Report what you find. Do not modify any files.",
            "",
            "Return every finding with its file, line, category "
            "(security | logic | typo | ux | reliability), severity "
            "(low | medium | high | critical) and a one-sentence summary.",
        ]
    )


def _standards_text(cfg: ReviewConfig) -> str:
    """The repo's CLAUDE.md, read as text for injection.

    Turning discovery off is only half an answer. `setting_sources=[]` with
    `inject_standards=False` is the configuration where the standards quietly
    vanish and the review silently gets worse.
    """
    path = cfg.repo / "CLAUDE.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def build_options(cfg: ReviewConfig) -> ClaudeAgentOptions:
    system_prompt: Any = {"type": "preset", "preset": "claude_code"}
    if cfg.inject_standards:
        standards = _standards_text(cfg)
        if standards:
            system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": (
                    "The following are this repository's review standards. "
                    "Apply them as if you had read them from the repo:\n\n" + standards
                ),
            }

    return ClaudeAgentOptions(
        cwd=str(cfg.repo),
        setting_sources=cfg.setting_sources,
        system_prompt=system_prompt,
        tools=cfg.tools,
        allowed_tools=cfg.tools,
        # CI runs with setting_sources=[], so .claude/settings.json is NOT
        # discovered. It is loaded by explicit path instead, in both modes —
        # otherwise the secret exclusion would silently work only locally, which
        # is exactly the failure mode a lean CI config invites.
        settings=str(SETTINGS_FILE),
        # "dontAsk": never prompt, deny anything not pre-approved. Anything that
        # can prompt hangs a headless run forever; "bypassPermissions" would
        # defeat the deny rules in that settings file.
        permission_mode="dontAsk",
        model=cfg.model,
        max_budget_usd=cfg.max_budget_usd,
        output_format={"type": "json_schema", "schema": FINDINGS_SCHEMA},
    )


def describe_options(cfg: ReviewConfig) -> str:
    """Copyable Python, for narrating the configuration in a demo."""
    opts = build_options(cfg)
    append = ""
    if isinstance(opts.system_prompt, dict) and opts.system_prompt.get("append"):
        append = f"{{preset: claude_code, append: <{len(opts.system_prompt['append'])} chars>}}"
    else:
        append = "{preset: claude_code}"
    return "\n".join(
        [
            "ClaudeAgentOptions(",
            f"    cwd={cfg.repo.name!r},",
            f"    setting_sources={opts.setting_sources!r},",
            f"    tools={opts.tools!r},",
            f"    allowed_tools={opts.allowed_tools!r},",
            f"    settings={'.claude/settings.json'!r},   # deny rules, loaded by path",
            f"    permission_mode={opts.permission_mode!r},",
            f"    model={opts.model!r},",
            f"    max_budget_usd={opts.max_budget_usd!r},",
            f"    system_prompt={append},",
            "    output_format={'type': 'json_schema', 'schema': FINDINGS_SCHEMA},",
            ")",
        ]
    )


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def _summarise_input(data: dict[str, Any]) -> str:
    for key in ("file_path", "path", "pattern", "command"):
        if key in data:
            value = str(data[key])
            return value if len(value) <= 70 else value[:67] + "..."
    return ", ".join(sorted(data)) if data else ""


def _extract_findings(result: ResultMessage) -> list[dict[str, Any]]:
    payload = result.structured_output
    if payload is None and result.result:
        try:
            payload = json.loads(result.result)
        except (ValueError, TypeError):
            return []
    if isinstance(payload, dict) and isinstance(payload.get("findings"), list):
        return [f for f in payload["findings"] if isinstance(f, dict)]
    return []


async def run_review(cfg: ReviewConfig) -> ReviewRun:
    run = ReviewRun(config=cfg)
    pending: dict[str, ToolEvent] = {}
    started = time.perf_counter()
    # The transport raises with the ResultMessage subtype, which for a billing
    # or auth failure is the word "success" — useless in a CI log. The real
    # reason arrives one message earlier, on the assistant turn. Keep it.
    reason: str | None = None

    try:
        async for message in query(prompt=build_prompt(cfg), options=build_options(cfg)):
            if isinstance(message, AssistantMessage):
                if getattr(message, "error", None):
                    detail = " ".join(
                        b.text for b in message.content if isinstance(b, TextBlock)
                    ).strip()
                    reason = f"{message.error}: {detail}" if detail else str(message.error)
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        event = ToolEvent(
                            name=block.name,
                            tool_use_id=block.id,
                            input_summary=_summarise_input(block.input),
                        )
                        run.events.append(event)
                        pending[block.id] = event
                    elif isinstance(block, TextBlock):
                        run.text += block.text

            elif isinstance(message, UserMessage):
                if not isinstance(message.content, list):
                    continue
                for block in message.content:
                    if not (isinstance(block, ToolResultBlock) and block.is_error):
                        continue
                    event = pending.get(block.tool_use_id)
                    if event is None:
                        continue
                    body = block.content
                    if isinstance(body, list):
                        body = " ".join(
                            str(p.get("text", "")) for p in body if isinstance(p, dict)
                        )
                    body = str(body or "")
                    event.is_error = True
                    # Source 1 of 2 for denials.
                    if _DENIAL_RE.search(body):
                        event.allowed = False
                        event.denied_reason = body.strip()[:200]

            elif isinstance(message, ResultMessage):
                run.session_id = message.session_id
                run.turns = message.num_turns
                run.cost_usd = message.total_cost_usd or 0.0
                run.findings = _extract_findings(message)
                if message.is_error:
                    run.is_error = True
                    run.error = reason or str(message.subtype)
                # Source 2 of 2 for denials.
                for denial in message.permission_denials or []:
                    if not isinstance(denial, dict):
                        continue
                    event = pending.get(denial.get("tool_use_id", ""))
                    if event is not None:
                        event.allowed = False
                        event.denied_reason = event.denied_reason or "permission denied"
                    else:
                        run.events.append(
                            ToolEvent(
                                name=denial.get("tool_name", "?"),
                                tool_use_id=denial.get("tool_use_id", ""),
                                input_summary=_summarise_input(denial.get("tool_input") or {}),
                                allowed=False,
                                denied_reason="permission denied",
                            )
                        )

    except Exception as exc:  # noqa: BLE001 — surfaced as exit code 2, never swallowed
        run.is_error = True
        run.error = reason or f"{type(exc).__name__}: {exc}"

    run.wall_clock_s = time.perf_counter() - started
    return run


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_text(run: ReviewRun) -> str:
    cfg = run.config
    out = ["", "=" * 72, f"  AI Feature Review: {cfg.feature}   [mode: {cfg.mode}]", "=" * 72]

    if run.is_error:
        out += ["", f"  REVIEW FAILED: {run.error}",
                "  This is a reviewer failure, not a verdict on the code.", ""]
        return "\n".join(out)

    findings = sorted(
        run.findings,
        key=lambda f: (SEVERITY_ORDER.get(str(f.get("severity", "")).lower(), 9),
                       str(f.get("file", ""))),
    )

    if not findings:
        out += ["", "  No findings above the reporting threshold."]
    else:
        out += ["", f"  {len(findings)} finding(s):", ""]
        for f in findings:
            gate = "  <- blocks merge" if f in run.blocking else ""
            out.append(f"  [{str(f.get('severity', '?')).upper():<8}] "
                       f"{f.get('file')}:{f.get('line')}  ({f.get('category')}){gate}")
            out.append(f"             {f.get('summary')}")
            out.append("")

    if run.denied:
        out.append("  Denied tool calls:")
        for e in run.denied:
            out.append(f"    x {e.name}({e.input_summary}) — {e.denied_reason}")
        out.append("")

    out += [
        "-" * 72,
        f"  tools used: {', '.join(run.tools_used) or 'none'}",
        f"  {run.turns} turns | {run.wall_clock_s:.1f}s | ${run.cost_usd:.4f} | "
        f"session {run.session_id}",
        f"  exit code: {run.exit_code()}",
        "=" * 72,
        "",
    ]
    return "\n".join(out)


def render_json(run: ReviewRun) -> str:
    return json.dumps(
        {
            "feature": run.config.feature,
            "mode": run.config.mode,
            "ok": not run.is_error,
            "error": run.error,
            "findings": run.findings,
            "blocking": run.blocking,
            "denied_calls": [
                {"tool": e.name, "input": e.input_summary, "reason": e.denied_reason}
                for e in run.denied
            ],
            "tools_used": run.tools_used,
            "turns": run.turns,
            "wall_clock_s": round(run.wall_clock_s, 2),
            "cost_usd": run.cost_usd,
            "session_id": run.session_id,
            "exit_code": run.exit_code(),
        },
        indent=2,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="review_cli.py",
        description="AI Feature Review for the Inkwell landing page.",
    )
    parser.add_argument("--feature", default="landing_page", choices=sorted(FEATURES))
    parser.add_argument("--mode", default="ci", choices=("local", "ci"))
    parser.add_argument("--format", dest="fmt", default=None, choices=("text", "json"),
                        help="default: text for local, json for ci")
    parser.add_argument("--out", type=Path, default=None,
                        help="also write the JSON report to this path")
    parser.add_argument("--show-options", action="store_true",
                        help="print the configuration and prompt, then exit without calling the API")
    args = parser.parse_args(argv)

    cfg = ReviewConfig.for_mode(args.mode, feature=args.feature)

    if args.show_options:
        print(describe_options(cfg))
        print("\n--- prompt ---\n")
        print(build_prompt(cfg))
        return 0

    run = asyncio.run(run_review(cfg))

    fmt = args.fmt or ("json" if args.mode == "ci" else "text")
    print(render_json(run) if fmt == "json" else render_text(run))

    if args.out:
        args.out.write_text(render_json(run), encoding="utf-8")

    return run.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
