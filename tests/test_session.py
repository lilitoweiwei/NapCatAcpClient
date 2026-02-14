"""Tests for the session management module."""

import pytest

from ncat.session import SessionManager

pytestmark = pytest.mark.asyncio


async def test_create_session(session_manager: SessionManager) -> None:
    """Test creating a new session."""
    session = await session_manager.create_session("group:123")
    assert session.chat_id == "group:123"
    assert session.status == "active"
    assert session.opencode_session_id is None
    assert session.id  # should have a UUID


async def test_get_active_session(session_manager: SessionManager) -> None:
    """Test retrieving an active session."""
    created = await session_manager.create_session("private:456")
    found = await session_manager.get_active_session("private:456")
    assert found is not None
    assert found.id == created.id
    assert found.status == "active"


async def test_get_active_session_not_found(session_manager: SessionManager) -> None:
    """Test that get_active_session returns None when no session exists."""
    found = await session_manager.get_active_session("group:nonexistent")
    assert found is None


async def test_archive_active_session(session_manager: SessionManager) -> None:
    """Test archiving an active session."""
    await session_manager.create_session("group:789")
    archived = await session_manager.archive_active_session("group:789")
    assert archived is True

    # Should not be found as active anymore
    found = await session_manager.get_active_session("group:789")
    assert found is None


async def test_archive_returns_false_when_none_active(
    session_manager: SessionManager,
) -> None:
    """Test that archive returns False when no active session exists."""
    archived = await session_manager.archive_active_session("group:nobody")
    assert archived is False


async def test_new_session_after_archive(session_manager: SessionManager) -> None:
    """Test creating a new session after archiving the previous one."""
    s1 = await session_manager.create_session("group:100")
    await session_manager.archive_active_session("group:100")
    s2 = await session_manager.create_session("group:100")

    assert s1.id != s2.id
    active = await session_manager.get_active_session("group:100")
    assert active is not None
    assert active.id == s2.id


async def test_update_opencode_session_id(session_manager: SessionManager) -> None:
    """Test updating the OpenCode session ID."""
    session = await session_manager.create_session("private:111")
    assert session.opencode_session_id is None

    await session_manager.update_opencode_session_id(session.id, "ses_abc123")

    updated = await session_manager.get_active_session("private:111")
    assert updated is not None
    assert updated.opencode_session_id == "ses_abc123"


async def test_only_one_active_per_chat(session_manager: SessionManager) -> None:
    """Test that creating a second session without archiving gives two active (edge case)."""
    # Note: The application logic should archive before creating new,
    # but the database layer itself doesn't enforce uniqueness on active status.
    await session_manager.create_session("group:200")
    s2 = await session_manager.create_session("group:200")

    # get_active_session returns the most recent one
    active = await session_manager.get_active_session("group:200")
    assert active is not None
    assert active.id == s2.id


async def test_init_idempotent(session_manager: SessionManager) -> None:
    """Test that calling init() multiple times doesn't fail."""
    # init() was already called by the fixture, call again
    await session_manager.init()
    # Should still work
    session = await session_manager.create_session("private:999")
    assert session is not None
