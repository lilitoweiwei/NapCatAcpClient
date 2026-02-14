"""Message conversion between OneBot 11 format and internal representation."""

from ncat.models import ContentPart, ImageAttachment, ParsedMessage


def onebot_to_internal(event: dict, bot_id: int) -> ParsedMessage:
    """
    Parse an OneBot 11 message event into a structured ParsedMessage.

    Args:
        event: The raw OneBot message event dict
        bot_id: The bot's own QQ ID (from self_id)
    """
    message_type: str = event.get("message_type", "")
    user_id: int = event.get("user_id", 0)
    group_id: int = event.get("group_id", 0)
    group_name: str | None = event.get("group_name")
    segments: list[dict] = event.get("message", [])
    sender: dict = event.get("sender", {})

    # Determine chat_id based on message type
    if message_type == "private":
        chat_id = f"private:{user_id}"
    else:
        chat_id = f"group:{group_id}"

    # Determine display name: prefer card (group nickname), fallback to nickname
    sender_name = sender.get("card") or sender.get("nickname", str(user_id))

    # Parse message segments into plain text and detect @bot
    text_parts: list[str] = []
    is_at_bot = False
    images: list[ImageAttachment] = []

    for seg in segments:
        seg_type = seg.get("type", "")
        seg_data = seg.get("data", {})

        if seg_type == "text":
            text_parts.append(seg_data.get("text", ""))

        elif seg_type == "at":
            # data.qq is a STRING in NapCatQQ, bot_id is int
            qq_str = str(seg_data.get("qq", ""))
            if qq_str == str(bot_id):
                is_at_bot = True
                # Skip @bot itself in the text output
            else:
                # Include other @mentions as text
                text_parts.append(f"@{qq_str}")

        elif seg_type == "image":
            text_parts.append("[图片]")
            url = str(seg_data.get("url", "")).strip()
            # Preserve image ordering: keep a placeholder entry even if URL is missing.
            images.append(ImageAttachment(url=url))

        elif seg_type == "face":
            text_parts.append("[表情]")
        # Other segment types (reply, etc.) are silently ignored

    text = "".join(text_parts).strip()

    return ParsedMessage(
        chat_id=chat_id,
        text=text,
        is_at_bot=is_at_bot,
        sender_name=sender_name,
        sender_id=user_id,
        group_name=group_name,
        message_type=message_type,
        images=images,
    )


def content_to_onebot(parts: list[ContentPart]) -> list[dict]:
    """Convert ordered ContentParts to OneBot 11 message segments."""
    segments: list[dict] = []
    for part in parts:
        if part.type == "text":
            if part.text:
                segments.append({"type": "text", "data": {"text": part.text}})
        elif part.type == "image":
            # NapCat/OneBot accepts base64 payloads via the "file" field.
            if part.image_base64:
                segments.append(
                    {"type": "image", "data": {"file": f"base64://{part.image_base64}"}}
                )
            else:
                segments.append({"type": "text", "data": {"text": "[图片]"}})

    # Ensure we always return a non-empty segment list.
    return segments or [{"type": "text", "data": {"text": ""}}]


def ai_to_onebot(text: str) -> list[dict]:
    """
    Convert AI response text to OneBot 11 message segment array.

    v1 simply wraps the text in a single text segment.
    """
    return content_to_onebot([ContentPart(type="text", text=text)])
