"""ACP prompt construction — builds prompt content blocks from parsed messages.

Knows about ACP content block types (text, image) and the context header format.
Used by `prompt_runner.py` to assemble the final prompt sent to the agent.
"""

import logging

from acp import image_block, text_block
from acp.schema import ImageContentBlock, TextContentBlock

from ncat.models import ParsedMessage

logger = logging.getLogger("ncat.prompt_builder")


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
