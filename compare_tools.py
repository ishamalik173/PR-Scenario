"""Does the wording of a tool description change which tool gets used?

    python compare_tools.py                        # run both versions, compare
    python compare_tools.py --descriptions detailed  # just one
    python compare_tools.py --show-options         # settings only, no API call

The same refactoring job, run twice against the same code, with the same tools
available. Only one thngs changes between the two runs: the wording of the MCP
tool descriptions.

    vague     "Extracts a function from code."
    detailed  what it does, when to use it, when NOT to, why to prefer it
              over Edit, the parameters, and a worked example

What we watch is which tools actually got called. Did the agent use the MCP
refactor tools, or reach for `Edit` and `Grep` instead?

This has nothing to do with the PR review. `review_cli.py` stays read-only with
no MCP server — a reviewer that never refactors would never call these tools,
so both runs would come back with zero calls and show nothing either way.

Each run gets its own throwaway copy of the files, so the two runs cannot
affect each other and your real files are never touched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env.local")
except ImportError:  # pragma: no cover
    pass

import refactor_tools  # noqa: E402
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_MODEL = os.environ.get("REVIEW_MODEL", "haiku")

# The job. It needs both tools, and both are things `Edit` could also do —
# that is what makes the choice worth watching rather than forced.
JOB = (
    "Two changes to `site/scripts.js`:\n"
    "\n"
    "1. The newsletter submit handler validates the email inline. Pull that "
    "validation out into its own top-level function called `isValidEmail`.\n"
    "2. Rename the `status` variable to `statusEl` everywhere it appears.\n"
    "\n"
    "Show me the resulting change."
)

# Copied into each run's scratch folder, keeping the site/ layout so paths in
# the job, the tool arguments and `Edit` all mean the same thing.
SITE = "site"
COPIED_FILES = ("CLAUDE.md",)

MCP_TOOLS = ["mcp__refactor__extract_function", "mcp__refactor__rename_symbol"]
BUILTIN_TOOLS = ["Read", "Grep", "Glob", "Edit"]


@dataclass
class RunResult:
    descriptions: str
    tools_called: list[str] = field(default_factory=list)
    text: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    wall_clock_s: float = 0.0
    error: str | None = None

    @property
    def mcp_calls(self) -> list[str]:
        return [t for t in self.tools_called if t.startswith("mcp__refactor__")]

    @property
    def edit_calls(self) -> list[str]:
        return [t for t in self.tools_called if t in ("Edit", "Write")]

    @property
    def used_mcp(self) -> bool:
        return bool(self.mcp_calls)


def build_options(descriptions: str, cwd: Path) -> ClaudeAgentOptions:
    """Identical in every respect except which set of descriptions is loaded."""
    server = refactor_tools.build_refactor_server(descriptions)
    return ClaudeAgentOptions(
        cwd=str(cwd),
        setting_sources=[],
        # `Edit` is here on purpose. This only tells us anything if the agent
        # has a real alternative to the MCP tools.
        tools=BUILTIN_TOOLS,
        allowed_tools=BUILTIN_TOOLS + MCP_TOOLS,
        mcp_servers={"refactor": server},
        # Without this, a stray project-level MCP config could add servers and
        # the two runs would no longer be comparable.
        strict_mcp_config=True,
        permission_mode="dontAsk",
        model=DEFAULT_MODEL,
        max_budget_usd=0.75,
    )


def _make_scratch_copy(name: str, parent: Path) -> Path:
    """A throwaway copy of the website, one per run."""
    folder = parent / name
    folder.mkdir(parents=True)
    shutil.copytree(REPO / SITE, folder / SITE)
    for filename in COPIED_FILES:
        source = REPO / filename
        if source.exists():
            shutil.copy2(source, folder / filename)
    return folder


async def run_once(descriptions: str, folder: Path) -> RunResult:
    result = RunResult(descriptions=descriptions)
    # The MCP tools read from disk, so point them at this run's copy too.
    # Otherwise they would report on the real files while `Edit` rewrote a copy.
    previous_root = refactor_tools.ROOT
    refactor_tools.ROOT = folder
    started = time.perf_counter()
    reason: str | None = None

    try:
        async for message in query(prompt=JOB, options=build_options(descriptions, folder)):
            if isinstance(message, AssistantMessage):
                if getattr(message, "error", None):
                    detail = " ".join(
                        b.text for b in message.content if isinstance(b, TextBlock)
                    ).strip()
                    reason = f"{message.error}: {detail}" if detail else str(message.error)
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        result.tools_called.append(block.name)
                    elif isinstance(block, TextBlock):
                        result.text += block.text
            elif isinstance(message, ResultMessage):
                result.turns = message.num_turns
                result.cost_usd = message.total_cost_usd or 0.0
                if message.is_error:
                    result.error = reason or str(message.subtype)
    except Exception as exc:  # noqa: BLE001
        result.error = reason or f"{type(exc).__name__}: {exc}"
    finally:
        refactor_tools.ROOT = previous_root

    result.wall_clock_s = time.perf_counter() - started
    return result


def render(results: list[RunResult]) -> str:
    out = ["", "=" * 72,
           "  Does the wording of a tool description change which tool gets used?",
           "=" * 72, ""]

    for r in results:
        label = f"{r.descriptions} descriptions"
        out.append(f"  --- {label} " + "-" * (60 - len(label)))
        if r.error:
            out += [f"      FAILED: {r.error}", ""]
            continue
        out.append(f"      tools called : {' -> '.join(r.tools_called) or '(none)'}")
        out.append(f"      MCP calls    : {len(r.mcp_calls)}")
        out.append(f"      Edit/Write   : {len(r.edit_calls)}")
        out.append(f"      {r.turns} turns | {r.wall_clock_s:.1f}s | ${r.cost_usd:.4f}")
        out.append("")

    finished = [r for r in results if not r.error]
    if len(finished) == 2:
        vague, detailed = finished[0], finished[1]
        out.append("-" * 72)
        if detailed.used_mcp and not vague.used_mcp:
            out.append("  RESULT: the wording mattered. The detailed run used the MCP tools;")
            out.append("          the vague run fell back to the built-in ones.")
        elif detailed.used_mcp and vague.used_mcp:
            out.append("  RESULT: both runs used the MCP tools. The job may be specific")
            out.append("          enough that the tool names alone were enough.")
        elif not detailed.used_mcp and not vague.used_mcp:
            out.append("  RESULT: neither run used the MCP tools. Check the tools are")
            out.append("          actually reachable before reading anything into that.")
        else:
            out.append("  RESULT: the vague run used them and the detailed run did not.")
            out.append("          Worth running again; one result is not a finding.")
        out.append("")

    out += ["  One run is a story, not evidence. Repeat before claiming a result.",
            "=" * 72, ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compare_tools.py",
        description="Compare vague vs detailed MCP tool descriptions.",
    )
    parser.add_argument("--descriptions", default=None, choices=("vague", "detailed"),
                        help="run just one of the two (default: run both)")
    parser.add_argument("--format", dest="fmt", default="text", choices=("text", "json"))
    parser.add_argument("--show-options", action="store_true",
                        help="print the settings and the job, then exit without calling the API")
    args = parser.parse_args(argv)

    if not (REPO / SITE / "scripts.js").exists():
        print(
            f"{SITE}/scripts.js not found.\n"
            "This rewrites the landing page's JavaScript, which only exists on the\n"
            "`test` branch. Run `git checkout test` first.",
            file=sys.stderr,
        )
        return 2

    wanted = [args.descriptions] if args.descriptions else ["vague", "detailed"]

    if args.show_options:
        for name in wanted:
            print(f"--- {name} descriptions ---")
            opts = build_options(name, REPO)
            print(f"  tools             = {opts.tools}")
            print(f"  allowed_tools     = {opts.allowed_tools}")
            print(f"  mcp_servers       = {{'refactor': <{name} descriptions>}}")
            print(f"  strict_mcp_config = {opts.strict_mcp_config}")
            print(f"  setting_sources   = {opts.setting_sources}")
            for tool_name, text in refactor_tools.DESCRIPTION_SETS[name].items():
                print(f"  {tool_name}: {len(text)} chars")
            print()
        print("--- the job ---\n")
        print(JOB)
        return 0

    results: list[RunResult] = []
    with tempfile.TemporaryDirectory(prefix="tool-compare-") as tmp:
        for name in wanted:
            folder = _make_scratch_copy(name, Path(tmp))
            results.append(asyncio.run(run_once(name, folder)))

    if args.fmt == "json":
        print(json.dumps(
            [
                {
                    "descriptions": r.descriptions,
                    "tools_called": r.tools_called,
                    "mcp_calls": r.mcp_calls,
                    "edit_calls": r.edit_calls,
                    "used_mcp": r.used_mcp,
                    "turns": r.turns,
                    "cost_usd": r.cost_usd,
                    "wall_clock_s": round(r.wall_clock_s, 2),
                    "error": r.error,
                }
                for r in results
            ],
            indent=2,
        ))
    else:
        print(render(results))

    return 2 if any(r.error for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
