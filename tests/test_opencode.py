"""Tests for the OpenCode backend module."""

import asyncio
import json

import pytest

from ncat.opencode import OpenCodeResponse, SubprocessOpenCodeBackend, parse_jsonl_events

# --- parse_jsonl_events unit tests ---


def _make_event(event_type: str, session_id: str = "ses_test123", **kwargs) -> str:
    """Helper to create a JSONL event line."""
    event: dict = {"type": event_type, "sessionID": session_id}
    event.update(kwargs)
    return json.dumps(event)


def test_parse_simple_text_response() -> None:
    """Test parsing a simple text-only response."""
    lines = [
        _make_event("step_start", part={"type": "step-start"}),
        _make_event("text", part={"text": "Hello world", "type": "text"}),
        _make_event(
            "step_finish",
            part={"reason": "stop", "cost": 0.001, "tokens": {"input": 10, "output": 5}},
        ),
    ]
    result = parse_jsonl_events(lines)
    assert result.session_id == "ses_test123"
    assert result.content == "Hello world"
    assert result.success is True
    assert result.error is None


def test_parse_multi_text_parts() -> None:
    """Test that multiple text events are concatenated."""
    lines = [
        _make_event("step_start", part={"type": "step-start"}),
        _make_event("text", part={"text": "Part 1", "type": "text"}),
        _make_event("text", part={"text": " Part 2", "type": "text"}),
        _make_event("step_finish", part={"reason": "stop"}),
    ]
    result = parse_jsonl_events(lines)
    assert result.content == "Part 1 Part 2"


def test_parse_with_tool_calls() -> None:
    """Test parsing response with tool calls before text."""
    lines = [
        _make_event("step_start", part={"type": "step-start"}),
        _make_event(
            "tool_use",
            part={
                "tool": "bash",
                "state": {"status": "completed", "output": "ok\n", "title": "Run test"},
            },
        ),
        _make_event("step_finish", part={"reason": "tool-calls"}),
        _make_event("step_start", part={"type": "step-start"}),
        _make_event("text", part={"text": "Done!", "type": "text"}),
        _make_event("step_finish", part={"reason": "stop"}),
    ]
    result = parse_jsonl_events(lines)
    assert result.content == "Done!"
    assert result.success is True


def test_parse_error_event() -> None:
    """Test parsing a response with an error event."""
    lines = [
        _make_event("step_start", part={"type": "step-start"}),
        _make_event(
            "error", error={"name": "APIError", "data": {"message": "Rate limit exceeded"}}
        ),
    ]
    result = parse_jsonl_events(lines)
    assert result.success is False
    assert "Rate limit exceeded" in result.error


def test_parse_empty_lines() -> None:
    """Test parsing with empty/blank lines."""
    lines = ["", "  ", _make_event("step_start", part={"type": "step-start"}), ""]
    result = parse_jsonl_events(lines)
    assert result.session_id == "ses_test123"


def test_parse_no_text() -> None:
    """Test parsing when there's no text event."""
    lines = [
        _make_event("step_start", part={"type": "step-start"}),
        _make_event("step_finish", part={"reason": "stop"}),
    ]
    result = parse_jsonl_events(lines)
    assert result.content == ""
    assert result.success is True


def test_parse_invalid_json_line() -> None:
    """Test that invalid JSON lines are skipped gracefully."""
    lines = [
        "not json at all",
        _make_event("text", part={"text": "Valid", "type": "text"}),
    ]
    result = parse_jsonl_events(lines)
    assert result.content == "Valid"


# --- SubprocessOpenCodeBackend tests (async) ---


@pytest.mark.asyncio
async def test_is_queue_full() -> None:
    """Test queue full detection."""
    backend = SubprocessOpenCodeBackend(command="echo", work_dir=".", max_concurrent=1)
    assert backend.is_queue_full() is False


@pytest.mark.asyncio
async def test_command_not_found() -> None:
    """Test handling of a non-existent command."""
    backend = SubprocessOpenCodeBackend(
        command="nonexistent_opencode_binary_xyz",
        work_dir=".",
        max_concurrent=1,
    )
    result = await backend.send_message(None, "test")
    assert result.success is False
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_concurrency_limit() -> None:
    """Test that concurrency is limited by the semaphore."""
    # Use 'sleep' equivalent: python -c "import time; time.sleep(X)"
    # With max_concurrent=1, the second call should wait
    backend = SubprocessOpenCodeBackend(
        command="python",  # will fail but that's ok for this test
        work_dir=".",
        max_concurrent=1,
    )

    # After starting one task, queue should report full
    started = asyncio.Event()

    async def slow_run(session_id, message):
        started.set()
        await asyncio.sleep(0.5)
        return OpenCodeResponse(session_id="ses_test", content="ok", success=True, error=None)

    backend._run = slow_run  # type: ignore

    # Start first task
    task1 = asyncio.create_task(backend.send_message(None, "msg1"))
    await started.wait()

    # Queue should now be full
    assert backend.is_queue_full() is True

    # Let it finish
    await task1
    assert backend.is_queue_full() is False
