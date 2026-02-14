"""Session management with SQLite persistence."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

logger = logging.getLogger("ncat.session")


@dataclass
class Session:
    """Represents a ncat session linking a QQ chat to an OpenCode session."""

    # Internal session UUID (ncat-generated)
    id: str
    # Chat identifier: "private:<user_id>" or "group:<group_id>"
    chat_id: str
    # Corresponding OpenCode session ID (format: ses_XXX); None until first AI call
    opencode_session_id: str | None
    # Session status: "active" (current) or "archived" (replaced by /new)
    status: str
    # ISO 8601 creation timestamp
    created_at: str
    # ISO 8601 last-update timestamp
    updated_at: str


class SessionManager:
    """Manages session lifecycle and persistence via SQLite."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        # Database connection; initialized by init(), closed by close()
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open database connection and create tables if needed."""
        self._db = await aiosqlite.connect(self._db_path)

        # Enable WAL mode for crash safety and better concurrency
        await self._db.execute("PRAGMA journal_mode=WAL")

        # Create sessions table
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                opencode_session_id TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Index for fast lookup of active session by chat_id
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_chat_status
            ON sessions (chat_id, status)
        """)

        await self._db.commit()
        logger.info("Database initialized at %s", self._db_path)

    async def get_active_session(self, chat_id: str) -> Session | None:
        """Get the active session for a chat_id, or None if none exists."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT id, chat_id, opencode_session_id, status, created_at, updated_at "
            "FROM sessions WHERE chat_id = ? AND status = 'active' "
            "ORDER BY created_at DESC LIMIT 1",
            (chat_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return Session(*row)

    async def create_session(self, chat_id: str) -> Session:
        """Create a new active session for the given chat_id."""
        assert self._db is not None
        now = datetime.now(UTC).isoformat()
        session_id = str(uuid.uuid4())

        await self._db.execute(
            "INSERT INTO sessions"
            " (id, chat_id, opencode_session_id, status, created_at, updated_at)"
            " VALUES (?, ?, NULL, 'active', ?, ?)",
            (session_id, chat_id, now, now),
        )
        await self._db.commit()

        session = Session(
            id=session_id,
            chat_id=chat_id,
            opencode_session_id=None,
            status="active",
            created_at=now,
            updated_at=now,
        )
        logger.info("Created session %s for %s", session_id[:8], chat_id)
        return session

    async def archive_active_session(self, chat_id: str) -> bool:
        """
        Archive the active session for a chat_id.
        Returns True if a session was archived, False if none was active.
        """
        assert self._db is not None
        now = datetime.now(UTC).isoformat()

        cursor = await self._db.execute(
            "UPDATE sessions SET status = 'archived', updated_at = ? "
            "WHERE chat_id = ? AND status = 'active'",
            (now, chat_id),
        )
        await self._db.commit()
        archived = cursor.rowcount > 0
        if archived:
            logger.info("Archived active session for %s", chat_id)
        return archived

    async def update_opencode_session_id(self, session_id: str, opencode_session_id: str) -> None:
        """Fill in the OpenCode session ID after the first OpenCode call."""
        assert self._db is not None
        now = datetime.now(UTC).isoformat()

        await self._db.execute(
            "UPDATE sessions SET opencode_session_id = ?, updated_at = ? WHERE id = ?",
            (opencode_session_id, now, session_id),
        )
        await self._db.commit()
        logger.debug(
            "Updated session %s with opencode_session_id %s",
            session_id[:8],
            opencode_session_id,
        )

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None
            logger.info("Database connection closed")
