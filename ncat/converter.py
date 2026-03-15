"""Message conversion between OneBot 11 format and internal representation."""

import re

from ncat.models import ContentPart, FileAttachment, ImageAttachment, ParsedMessage


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
    files: list[FileAttachment] = []
    has_text = False

    for seg in segments:
        seg_type = seg.get("type", "")
        seg_data = seg.get("data", {})

        if seg_type == "text":
            seg_text = seg_data.get("text", "")
            text_parts.append(seg_text)
            if str(seg_text).strip():
                has_text = True

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

        elif seg_type == "file":
            text_parts.append("[文件]")
            if message_type == "private":
                size_raw = seg_data.get("file_size")
                size: int | None
                try:
                    size = int(size_raw) if size_raw not in (None, "") else None
                except (TypeError, ValueError):
                    size = None
                files.append(
                    FileAttachment(
                        name=str(seg_data.get("file", "")).strip(),
                        file_id=str(seg_data.get("file_id", "")).strip(),
                        url=str(seg_data.get("url", "")).strip(),
                        size=size,
                    )
                )

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
        files=files,
        has_text=has_text,
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


_NEWLINE_TOKEN_RE = re.compile(r"\r\n|\r|\n|[^\r\n]+")


def _normalize_split_start_length(max_text_length: int, split_start_length: int) -> int:
    if split_start_length <= 0 or split_start_length >= max_text_length:
        return max_text_length
    return split_start_length


def _append_text_part(batch: list[ContentPart], text: str) -> None:
    if not text:
        return
    if batch and batch[-1].type == "text":
        batch[-1].text += text
        return
    batch.append(ContentPart(type="text", text=text))


def split_text_for_onebot(
    text: str,
    max_text_length: int,
    split_start_length: int = 0,
) -> list[str]:
    """Split text into QQ-safe chunks, preferring a newline after a soft threshold."""
    text_batches = split_content_parts_for_onebot(
        [ContentPart(type="text", text=text)],
        max_text_length,
        split_start_length,
    )
    return ["".join(part.text for part in batch if part.type == "text") for batch in text_batches]


def split_content_parts_for_onebot(
    parts: list[ContentPart],
    max_text_length: int,
    split_start_length: int = 0,
) -> list[list[ContentPart]]:
    """Split ordered content parts into multiple outbound QQ messages."""
    if max_text_length <= 0:
        return [parts]
    if not parts:
        return [[]]

    split_start_length = _normalize_split_start_length(max_text_length, split_start_length)

    batches: list[list[ContentPart]] = []
    current_batch: list[ContentPart] = []
    current_text_length = 0

    def flush() -> None:
        nonlocal current_batch, current_text_length
        if current_batch:
            batches.append(current_batch)
            current_batch = []
            current_text_length = 0

    for part in parts:
        if part.type == "image":
            current_batch.append(part)
            continue

        if part.type != "text" or not part.text:
            continue

        for token_match in _NEWLINE_TOKEN_RE.finditer(part.text):
            token = token_match.group(0)

            if token in {"\n", "\r", "\r\n"} and current_text_length > split_start_length:
                flush()
                continue

            remaining_text = token
            while remaining_text:
                if current_text_length >= max_text_length:
                    flush()

                remaining_capacity = max_text_length - current_text_length
                text_chunk = remaining_text[:remaining_capacity]
                _append_text_part(current_batch, text_chunk)
                current_text_length += len(text_chunk)
                remaining_text = remaining_text[remaining_capacity:]

                if remaining_text:
                    flush()

    flush()
    return batches or [parts]


def content_to_onebot_batches(
    parts: list[ContentPart],
    max_text_length: int,
    split_start_length: int = 0,
) -> list[list[dict]]:
    """Convert content parts into one or more outbound OneBot message payloads."""
    return [
        content_to_onebot(batch)
        for batch in split_content_parts_for_onebot(parts, max_text_length, split_start_length)
    ]


def ai_to_onebot(text: str) -> list[dict]:
    """
    Convert AI response text to OneBot 11 message segment array.

    v1 simply wraps the text in a single text segment.
    """
    return content_to_onebot([ContentPart(type="text", text=text)])


def ai_to_onebot_batches(
    text: str,
    max_text_length: int,
    split_start_length: int = 0,
) -> list[list[dict]]:
    """Convert AI text into one or more outbound OneBot message payloads."""
    return [
        ai_to_onebot(chunk)
        for chunk in split_text_for_onebot(text, max_text_length, split_start_length)
    ]
