"""Tests for converter, models, and prompt_builder modules."""

from acp.schema import ImageContentBlock, TextContentBlock

from ncat.converter import ai_to_onebot, content_to_onebot, onebot_to_internal
from ncat.models import ContentPart
from ncat.prompt_builder import build_context_header, build_prompt_blocks

BOT_ID = 1234567890


# --- onebot_to_internal tests ---


def test_parse_private_text_message() -> None:
    """Test parsing a simple private text message."""
    event = {
        "self_id": BOT_ID,
        "user_id": 111222,
        "message_type": "private",
        "sender": {"user_id": 111222, "nickname": "Alice", "card": ""},
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "post_type": "message",
    }
    parsed = onebot_to_internal(event, BOT_ID)
    assert parsed.chat_id == "private:111222"
    assert parsed.text == "hello"
    assert parsed.sender_name == "Alice"
    assert parsed.sender_id == 111222
    assert parsed.message_type == "private"
    assert parsed.group_name is None
    assert parsed.is_at_bot is False


def test_parse_group_message_with_at_bot() -> None:
    """Test parsing a group message where the bot is @-mentioned."""
    event = {
        "self_id": BOT_ID,
        "user_id": 333444,
        "message_type": "group",
        "group_id": 999888,
        "group_name": "测试群",
        "sender": {"user_id": 333444, "nickname": "Bob", "card": "Bob群名片"},
        "message": [
            {"type": "at", "data": {"qq": str(BOT_ID)}},
            {"type": "text", "data": {"text": " 你好"}},
        ],
        "post_type": "message",
    }
    parsed = onebot_to_internal(event, BOT_ID)
    assert parsed.chat_id == "group:999888"
    assert parsed.text == "你好"
    assert parsed.is_at_bot is True
    assert parsed.group_name == "测试群"
    # Should prefer card over nickname
    assert parsed.sender_name == "Bob群名片"


def test_parse_group_message_without_at_bot() -> None:
    """Test that group messages without @bot have is_at_bot=False."""
    event = {
        "self_id": BOT_ID,
        "user_id": 333444,
        "message_type": "group",
        "group_id": 999888,
        "group_name": "测试群",
        "sender": {"user_id": 333444, "nickname": "Bob", "card": ""},
        "message": [{"type": "text", "data": {"text": "普通消息"}}],
        "post_type": "message",
    }
    parsed = onebot_to_internal(event, BOT_ID)
    assert parsed.is_at_bot is False
    assert parsed.text == "普通消息"


def test_parse_mixed_segments() -> None:
    """Test parsing a message with mixed segment types."""
    event = {
        "self_id": BOT_ID,
        "user_id": 111,
        "message_type": "private",
        "sender": {"user_id": 111, "nickname": "User", "card": ""},
        "message": [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"url": "http://example.com/img.jpg"}},
            {"type": "text", "data": {"text": "好看吗"}},
            {"type": "face", "data": {"id": "1"}},
        ],
        "post_type": "message",
    }
    parsed = onebot_to_internal(event, BOT_ID)
    assert parsed.text == "看这个[图片]好看吗[表情]"
    assert len(parsed.images) == 1
    assert parsed.images[0].url == "http://example.com/img.jpg"


def test_parse_at_other_user() -> None:
    """Test that @-mentioning a non-bot user is included as text."""
    event = {
        "self_id": BOT_ID,
        "user_id": 111,
        "message_type": "group",
        "group_id": 222,
        "group_name": "G",
        "sender": {"user_id": 111, "nickname": "U", "card": ""},
        "message": [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": " 你看看"}},
        ],
        "post_type": "message",
    }
    parsed = onebot_to_internal(event, BOT_ID)
    assert parsed.is_at_bot is False
    assert "@999" in parsed.text


def test_sender_name_prefers_card() -> None:
    """Test that card (group nickname) is preferred over nickname."""
    event = {
        "self_id": BOT_ID,
        "user_id": 111,
        "message_type": "group",
        "group_id": 222,
        "group_name": "G",
        "sender": {"user_id": 111, "nickname": "RealName", "card": "CardName"},
        "message": [{"type": "text", "data": {"text": "hi"}}],
        "post_type": "message",
    }
    parsed = onebot_to_internal(event, BOT_ID)
    assert parsed.sender_name == "CardName"


def test_sender_name_fallback_to_nickname() -> None:
    """Test fallback to nickname when card is empty."""
    event = {
        "self_id": BOT_ID,
        "user_id": 111,
        "message_type": "group",
        "group_id": 222,
        "group_name": "G",
        "sender": {"user_id": 111, "nickname": "RealName", "card": ""},
        "message": [{"type": "text", "data": {"text": "hi"}}],
        "post_type": "message",
    }
    parsed = onebot_to_internal(event, BOT_ID)
    assert parsed.sender_name == "RealName"


# --- ai_to_onebot tests ---


def test_ai_to_onebot() -> None:
    result = ai_to_onebot("Hello world")
    assert result == [{"type": "text", "data": {"text": "Hello world"}}]


def test_content_to_onebot_text_and_image() -> None:
    parts = [
        ContentPart(type="text", text="hi"),
        ContentPart(type="image", image_base64="aGVsbG8=", image_mime="image/png"),
        ContentPart(type="text", text="bye"),
    ]
    segments = content_to_onebot(parts)
    assert segments == [
        {"type": "text", "data": {"text": "hi"}},
        {"type": "image", "data": {"file": "base64://aGVsbG8="}},
        {"type": "text", "data": {"text": "bye"}},
    ]


def test_build_prompt_blocks_image_success() -> None:
    event = {
        "self_id": BOT_ID,
        "user_id": 111,
        "message_type": "private",
        "sender": {"user_id": 111, "nickname": "User", "card": ""},
        "message": [
            {"type": "text", "data": {"text": "see"}},
            {"type": "image", "data": {"url": "http://example.com/a.png"}},
        ],
        "post_type": "message",
    }
    parsed = onebot_to_internal(event, BOT_ID)

    blocks = build_prompt_blocks(
        parsed,
        downloaded_images=[("aGVsbG8=", "image/png")],
        agent_supports_image=True,
    )
    assert isinstance(blocks[0], TextContentBlock)
    assert "[Private chat, user User(111)]" in blocks[0].text
    assert "[图片]" in blocks[0].text
    assert any(isinstance(b, ImageContentBlock) for b in blocks)


def test_build_prompt_blocks_download_failed_falls_back_to_url() -> None:
    event = {
        "self_id": BOT_ID,
        "user_id": 111,
        "message_type": "private",
        "sender": {"user_id": 111, "nickname": "User", "card": ""},
        "message": [
            {"type": "text", "data": {"text": "see"}},
            {"type": "image", "data": {"url": "http://example.com/a.png"}},
        ],
        "post_type": "message",
    }
    parsed = onebot_to_internal(event, BOT_ID)

    blocks = build_prompt_blocks(
        parsed,
        downloaded_images=[None],
        agent_supports_image=True,
    )
    assert isinstance(blocks[0], TextContentBlock)
    assert "[图片 url=http://example.com/a.png]" in blocks[0].text
    assert not any(isinstance(b, ImageContentBlock) for b in blocks)


# --- build_context_header tests ---


def test_context_header_private() -> None:
    """Test context header for private messages."""
    event = {
        "self_id": BOT_ID,
        "user_id": 111,
        "message_type": "private",
        "sender": {"user_id": 111, "nickname": "Alice", "card": ""},
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "post_type": "message",
    }
    parsed = onebot_to_internal(event, BOT_ID)
    header = build_context_header(parsed)
    assert "[Private chat, user Alice(111)]" in header
    assert "hello" in header


def test_context_header_group() -> None:
    """Test context header for group messages."""
    event = {
        "self_id": BOT_ID,
        "user_id": 333,
        "message_type": "group",
        "group_id": 999,
        "group_name": "TestGroup",
        "sender": {"user_id": 333, "nickname": "Bob", "card": ""},
        "message": [{"type": "text", "data": {"text": "hi"}}],
        "post_type": "message",
    }
    parsed = onebot_to_internal(event, BOT_ID)
    header = build_context_header(parsed)
    assert "[Group chat TestGroup(999)" in header
    assert "user Bob(333)]" in header
    assert "hi" in header
