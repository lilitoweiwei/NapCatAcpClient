"""Shared data types used across ncat modules."""

from dataclasses import dataclass, field


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
class VisibleTurnEvent:
    """User-visible status event emitted at an ACP turn boundary."""

    # Stable dedupe key within the active turn
    key: str
    # User-visible status line to append below buffered content
    status_text: str
    # Count of content parts accumulated before this event was observed
    part_count: int = 0


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
