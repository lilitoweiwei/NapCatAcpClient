"""
OpenCode CLI verification script.

This script verifies that `opencode run --format json` works as expected:
1. Runs a simple prompt and parses the JSONL output
2. Extracts and displays session ID, text responses, tool calls, and finish events
3. Then runs a follow-up prompt using the session ID to verify session continuation

Usage:
    uv run python tests/verify_opencode.py

Prerequisites:
    - `opencode` CLI must be installed and configured (API key set up)
    - Run from a directory where opencode can operate
"""

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime


# Path to opencode executable (change if not in PATH)
OPENCODE_CMD = "opencode"

# Test prompts
FIRST_PROMPT = "Say exactly: 'Hello from OpenCode! The answer is 42.' Nothing else."
FOLLOWUP_PROMPT = "What number did you just mention? Reply with just the number."


def timestamp() -> str:
    """Return current timestamp string for log output."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


@dataclass
class RunResult:
    """Parsed result from an opencode run."""
    session_id: str | None = None
    text_parts: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    finished: bool = False
    finish_reason: str | None = None
    cost: float | None = None
    tokens: dict | None = None
    errors: list[str] = field(default_factory=list)
    raw_events: list[dict] = field(default_factory=list)


async def run_opencode(
    prompt: str, session_id: str | None = None
) -> RunResult:
    """
    Run opencode CLI with --format json and parse the JSONL output.

    Args:
        prompt: The prompt to send
        session_id: Optional session ID to continue a conversation
    Returns:
        Parsed RunResult with all extracted information
    """
    # Build command
    cmd = [OPENCODE_CMD, "run", "--format", "json"]
    if session_id:
        cmd.extend(["-s", session_id])
    cmd.append(prompt)

    print(f"\n[{timestamp()}] Running: {' '.join(cmd)}")
    print("-" * 60)

    # Start subprocess
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    result = RunResult()

    # Read stdout line by line (JSONL)
    assert process.stdout is not None
    line_num = 0
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        line_num += 1
        text = line.decode("utf-8").strip()
        if not text:
            continue

        # Parse JSON
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            print(f"  [line {line_num}] NON-JSON: {text}")
            continue

        result.raw_events.append(event)
        event_type = event.get("type", "unknown")

        # Extract session ID from any event
        if result.session_id is None and "sessionID" in event:
            result.session_id = event["sessionID"]
            print(f"  [line {line_num}] SESSION ID: {result.session_id}")

        # Handle each event type
        if event_type == "step_start":
            snapshot = event.get("part", {}).get("snapshot", "N/A")
            print(f"  [line {line_num}] STEP_START (snapshot: {snapshot[:12]}...)")

        elif event_type == "text":
            part_text = event.get("part", {}).get("text", "")
            result.text_parts.append(part_text)
            # Show text preview (truncate if long)
            preview = part_text[:200] + "..." if len(part_text) > 200 else part_text
            print(f"  [line {line_num}] TEXT: {preview!r}")

        elif event_type == "tool_use":
            part = event.get("part", {})
            tool_name = part.get("tool", "?")
            state = part.get("state", {})
            status = state.get("status", "?")
            title = state.get("title", "")
            result.tool_calls.append({"tool": tool_name, "status": status, "title": title})
            print(f"  [line {line_num}] TOOL_USE: {tool_name} ({status}) - {title}")

        elif event_type == "step_finish":
            part = event.get("part", {})
            reason = part.get("reason", "N/A")
            cost = part.get("cost")
            tokens = part.get("tokens")
            print(f"  [line {line_num}] STEP_FINISH: reason={reason}, cost={cost}")
            if tokens:
                print(f"               tokens: in={tokens.get('input')}, out={tokens.get('output')}, "
                      f"reasoning={tokens.get('reasoning')}, "
                      f"cache_read={tokens.get('cache', {}).get('read')}")
            # Only mark finished on final stop
            if reason == "stop":
                result.finished = True
                result.finish_reason = reason
                result.cost = cost
                result.tokens = tokens

        elif event_type == "error":
            error_data = event.get("error", {})
            error_msg = error_data.get("data", {}).get("message", str(error_data))
            result.errors.append(error_msg)
            print(f"  [line {line_num}] ERROR: {error_msg}")

        else:
            print(f"  [line {line_num}] UNKNOWN ({event_type}): {json.dumps(event, ensure_ascii=False)[:200]}")

    # Read stderr
    assert process.stderr is not None
    stderr_data = await process.stderr.read()
    stderr_text = stderr_data.decode("utf-8").strip()
    if stderr_text:
        print(f"\n  STDERR:\n{stderr_text}")

    # Wait for process to finish
    return_code = await process.wait()
    print(f"\n  Exit code: {return_code}")

    return result


def print_summary(label: str, result: RunResult) -> None:
    """Print a summary of the run result."""
    print(f"\n{'=' * 60}")
    print(f"  SUMMARY: {label}")
    print(f"{'=' * 60}")
    print(f"  Session ID:    {result.session_id}")
    full_text = "".join(result.text_parts)
    print(f"  AI Response:   {full_text!r}")
    print(f"  Tool calls:    {len(result.tool_calls)}")
    for tc in result.tool_calls:
        print(f"    - {tc['tool']}: {tc['title']}")
    print(f"  Finished:      {result.finished} (reason: {result.finish_reason})")
    print(f"  Cost:          {result.cost}")
    print(f"  Errors:        {result.errors if result.errors else 'None'}")
    print(f"{'=' * 60}")


async def main() -> None:
    """Run verification tests."""
    print(f"[{timestamp()}] OpenCode CLI Verification")
    print(f"[{timestamp()}] Testing: opencode run --format json")

    # --- Test 1: First prompt (new session) ---
    print(f"\n{'#' * 60}")
    print(f"  TEST 1: New session - first prompt")
    print(f"{'#' * 60}")

    result1 = await run_opencode(FIRST_PROMPT)
    print_summary("Test 1 - New Session", result1)

    if not result1.session_id:
        print("\nFAILED: Could not extract session ID. Cannot proceed to Test 2.")
        sys.exit(1)

    if not result1.text_parts:
        print("\nWARNING: No text output received. The AI may not have responded with text.")

    # --- Test 2: Follow-up prompt (continue session) ---
    print(f"\n{'#' * 60}")
    print(f"  TEST 2: Continue session - follow-up prompt")
    print(f"  Using session ID: {result1.session_id}")
    print(f"{'#' * 60}")

    result2 = await run_opencode(FOLLOWUP_PROMPT, session_id=result1.session_id)
    print_summary("Test 2 - Continue Session", result2)

    # --- Final verdict ---
    print(f"\n{'#' * 60}")
    print(f"  VERIFICATION RESULTS")
    print(f"{'#' * 60}")

    checks = [
        ("JSONL output parseable", len(result1.raw_events) > 0),
        ("Session ID extracted", result1.session_id is not None),
        ("Text response received (test 1)", len(result1.text_parts) > 0),
        ("Step finished with 'stop'", result1.finished),
        ("Session continuation works", result2.session_id == result1.session_id),
        ("Text response received (test 2)", len(result2.text_parts) > 0),
        ("No errors in test 1", len(result1.errors) == 0),
        ("No errors in test 2", len(result2.errors) == 0),
    ]

    all_passed = True
    for name, passed in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        print(f"  [{status}] {name}")

    print()
    if all_passed:
        print("  All checks passed! OpenCode CLI integration is working as expected.")
    else:
        print("  Some checks failed. Review the output above for details.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
