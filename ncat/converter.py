"""Message conversion between OneBot 11 format and internal representation."""

import logging
from dataclasses import dataclass, field

from acp import image_block, text_block
from acp.schema import ImageContentBlock, TextContentBlock

logger = logging.getLogger("ncat.converter")


@dataclass
class ImageAttachment:
    """Raw image reference extracted from OneBot message segments."""

    # Download URL provided by NapCat/OneBot (HTTP link)
    url: str


@dataclass
class ContentPart:
    """Ordered content part for AI replies (text and images)."""

    # "text" or "image"
    type: str
    # Text content (when type == "text")
    text: str = ""
    # Base64-encoded image bytes (when type == "image", without "base64://" prefix)
    image_base64: str = ""
    # MIME type for the image (e.g. "image/png")
    image_mime: str = ""


@dataclass
class ParsedMessage:
    """Result of parsing an incoming OneBot message event."""

    # Unique chat identifier: "private:<user_id>" or "group:<group_id>"
    chat_id: str
    # Plain text extracted from message segments (@bot stripped, images→placeholders)
    text: str
    # Whether the bot was @-mentioned in this message (always False for private)
    is_at_bot: bool
    # Display name of the sender (group card preferred, fallback to nickname)
    sender_name: str
    # QQ number of the message sender
    sender_id: int
    # Group name from the event payload (None for private messages)
    group_name: str | None
    # "private" or "group"
    message_type: str
    # Raw image attachments (URLs) extracted from the message segments
    images: list[ImageAttachment] = field(default_factory=list)


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


def build_context_header(parsed: ParsedMessage) -> str:
    """Build a context header with sender/group info for the ACP prompt.

    Prepended to the user's message text so the agent knows who is speaking
    and from which chat context (private vs group).
    """
    if parsed.message_type == "private":
        header = f"[Private chat, user {parsed.sender_name}({parsed.sender_id})]"
    else:
        group_id = parsed.chat_id.split(":")[1]
        header = (
            f"[Group chat {parsed.group_name}({group_id}), "
            f"user {parsed.sender_name}({parsed.sender_id})]"
        )
    return f"{header}\n{parsed.text}"


def _replace_image_placeholders(text: str, replacements: list[str]) -> str:
    """Replace each '[图片]' placeholder with a corresponding replacement string."""
    if not replacements:
        return text

    marker = "[图片]"
    placeholder_count = text.count(marker)
    if placeholder_count > len(replacements):
        logger.warning(
            "More image placeholders than attachments: placeholders=%d attachments=%d",
            placeholder_count,
            len(replacements),
        )

    out: list[str] = []
    start = 0
    used = 0
    while used < len(replacements):
        idx = text.find(marker, start)
        if idx == -1:
            break
        out.append(text[start:idx])
        out.append(replacements[used])
        used += 1
        start = idx + len(marker)
    out.append(text[start:])
    result = "".join(out)

    # If we have more attachments than placeholders, append the rest as extra context.
    if used < len(replacements):
        extra = "\n".join(replacements[used:])
        sep = "\n" if result and not result.endswith("\n") else ""
        result = f"{result}{sep}{extra}"
        logger.warning(
            "More attachments than image placeholders; appended extras: "
            "placeholders=%d attachments=%d",
            placeholder_count,
            len(replacements),
        )

    return result


def build_prompt_blocks(
    parsed: ParsedMessage,
    downloaded_images: list[tuple[str, str] | None],
    agent_supports_image: bool,
) -> list[TextContentBlock | ImageContentBlock]:
    """Build ACP prompt content blocks (text + optional images)."""
    # Decide how to represent each image in the text for agent-side fallback.
    replacements: list[str] = []
    for i, att in enumerate(parsed.images):
        downloaded = downloaded_images[i] if i < len(downloaded_images) else None
        if agent_supports_image and downloaded is not None:
            replacements.append("[图片]")
            continue

        url = att.url.strip()
        replacements.append(f"[图片 url={url}]" if url else "[图片]")

    body_text = _replace_image_placeholders(parsed.text, replacements)

    # Build a context header so the agent knows who is speaking and where.
    if parsed.message_type == "private":
        header = f"[Private chat, user {parsed.sender_name}({parsed.sender_id})]"
    else:
        group_id = parsed.chat_id.split(":")[1]
        header = (
            f"[Group chat {parsed.group_name}({group_id}), "
            f"user {parsed.sender_name}({parsed.sender_id})]"
        )

    blocks: list[TextContentBlock | ImageContentBlock] = [text_block(f"{header}\n{body_text}")]
    if agent_supports_image:
        for downloaded in downloaded_images:
            if downloaded is None:
                continue
            data_b64, mime_type = downloaded
            blocks.append(image_block(data_b64, mime_type))

    return blocks


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
