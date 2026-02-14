"""Tests for the PromptBuilder class."""

from pathlib import Path

from ncat.converter import onebot_to_internal
from ncat.prompt import PromptBuilder

BOT_ID = 1234567890


def _make_parsed(text: str = "hello", message_type: str = "private"):
    """Helper to create a ParsedMessage from a minimal event."""
    if message_type == "private":
        event = {
            "self_id": BOT_ID,
            "user_id": 111,
            "message_type": "private",
            "sender": {"user_id": 111, "nickname": "Alice", "card": ""},
            "message": [{"type": "text", "data": {"text": text}}],
            "post_type": "message",
        }
    else:
        event = {
            "self_id": BOT_ID,
            "user_id": 111,
            "message_type": "group",
            "group_id": 222,
            "group_name": "开发群",
            "sender": {"user_id": 111, "nickname": "Alice", "card": ""},
            "message": [
                {"type": "at", "data": {"qq": str(BOT_ID)}},
                {"type": "text", "data": {"text": f" {text}"}},
            ],
            "post_type": "message",
        }
    return onebot_to_internal(event, BOT_ID)


def test_build_prompt_private_no_files(tmp_path: Path) -> None:
    """With empty prompt files, output is just context header + message."""
    builder = PromptBuilder(tmp_path / "prompts")
    parsed = _make_parsed("写个函数")
    prompt = builder.build(parsed, is_new_session=True)
    assert "[私聊，用户 Alice(111)]" in prompt
    assert "写个函数" in prompt


def test_build_prompt_group_no_files(tmp_path: Path) -> None:
    """Group prompt includes group name and user info."""
    builder = PromptBuilder(tmp_path / "prompts")
    parsed = _make_parsed("帮忙", message_type="group")
    prompt = builder.build(parsed, is_new_session=False)
    assert "[群聊 开发群(222)" in prompt
    assert "用户 Alice(111)]" in prompt
    assert "帮忙" in prompt


def test_session_init_included_on_new_session(tmp_path: Path) -> None:
    """session_init content should appear only when is_new_session=True."""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "session_init.md").write_text("You are a helpful assistant.", encoding="utf-8")

    builder = PromptBuilder(prompt_dir)
    parsed = _make_parsed("hi")

    # New session: session_init should be included
    prompt_new = builder.build(parsed, is_new_session=True)
    assert "You are a helpful assistant." in prompt_new

    # Existing session: session_init should NOT be included
    prompt_old = builder.build(parsed, is_new_session=False)
    assert "You are a helpful assistant." not in prompt_old


def test_message_prefix_always_included(tmp_path: Path) -> None:
    """message_prefix content should appear on every message."""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "message_prefix.md").write_text("Reply in Chinese.", encoding="utf-8")

    builder = PromptBuilder(prompt_dir)
    parsed = _make_parsed("test")

    prompt_new = builder.build(parsed, is_new_session=True)
    assert "Reply in Chinese." in prompt_new

    prompt_old = builder.build(parsed, is_new_session=False)
    assert "Reply in Chinese." in prompt_old


def test_both_prompts_on_new_session(tmp_path: Path) -> None:
    """Both session_init and message_prefix appear, in correct order."""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "session_init.md").write_text("INIT_MARKER", encoding="utf-8")
    (prompt_dir / "message_prefix.md").write_text("PREFIX_MARKER", encoding="utf-8")

    builder = PromptBuilder(prompt_dir)
    parsed = _make_parsed("msg")
    prompt = builder.build(parsed, is_new_session=True)

    # Verify order: session_init before message_prefix before user message
    init_pos = prompt.index("INIT_MARKER")
    prefix_pos = prompt.index("PREFIX_MARKER")
    msg_pos = prompt.index("msg")
    assert init_pos < prefix_pos < msg_pos


def test_empty_files_are_skipped(tmp_path: Path) -> None:
    """Empty or whitespace-only prompt files produce no extra content."""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "session_init.md").write_text("   \n  ", encoding="utf-8")
    (prompt_dir / "message_prefix.md").write_text("", encoding="utf-8")

    builder = PromptBuilder(prompt_dir)
    parsed = _make_parsed("hello")
    prompt = builder.build(parsed, is_new_session=True)

    # Should just be the header + message, no extra blank sections
    assert prompt == "[私聊，用户 Alice(111)]\nhello"


def test_auto_creates_directory_and_files(tmp_path: Path) -> None:
    """PromptBuilder creates the prompt dir and empty files if they don't exist."""
    prompt_dir = tmp_path / "new" / "nested" / "prompts"
    assert not prompt_dir.exists()

    builder = PromptBuilder(prompt_dir)

    assert prompt_dir.exists()
    assert (prompt_dir / "session_init.md").exists()
    assert (prompt_dir / "message_prefix.md").exists()
    # Created files should be empty
    assert (prompt_dir / "session_init.md").read_text() == ""
    assert (prompt_dir / "message_prefix.md").read_text() == ""

    # Build should still work fine
    parsed = _make_parsed("test")
    prompt = builder.build(parsed, is_new_session=True)
    assert "test" in prompt


def test_custom_filenames(tmp_path: Path) -> None:
    """Custom file names should be respected."""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "SYSTEM.md").write_text("custom system prompt", encoding="utf-8")
    (prompt_dir / "ALWAYS.md").write_text("custom always prompt", encoding="utf-8")

    builder = PromptBuilder(
        prompt_dir,
        session_init_file="SYSTEM.md",
        message_prefix_file="ALWAYS.md",
    )
    parsed = _make_parsed("test")
    prompt = builder.build(parsed, is_new_session=True)

    assert "custom system prompt" in prompt
    assert "custom always prompt" in prompt


def test_hot_reload(tmp_path: Path) -> None:
    """Changing prompt files between calls should take effect immediately."""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    init_file = prompt_dir / "session_init.md"
    init_file.write_text("version1", encoding="utf-8")

    builder = PromptBuilder(prompt_dir)
    parsed = _make_parsed("test")

    prompt1 = builder.build(parsed, is_new_session=True)
    assert "version1" in prompt1

    # Modify the file
    init_file.write_text("version2", encoding="utf-8")

    prompt2 = builder.build(parsed, is_new_session=True)
    assert "version2" in prompt2
    assert "version1" not in prompt2
