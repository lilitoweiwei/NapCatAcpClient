"""BSP (Background Session Protocol) HTTP client.

Wraps all BSP API calls with proper error handling and type conversion.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger("ncat.bsp_client")


class BspClient:
    """BSP (Background Session Protocol) HTTP client.

    Wraps all BSP API calls with proper error handling and type conversion.
    """

    def __init__(self, base_url: str):
        """Initialize BSP client with base URL.

        Args:
            base_url: Base URL of the BSP server (e.g., "http://127.0.0.1:8766")
        """
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    # --- Session management ---

    async def create_session(
        self,
        prompt: str,
        notify_frontend: str,
        notify_chat: str,
        name: str | None = None,
    ) -> str:
        """Create a new background session.

        Args:
            prompt: Initial prompt to send to the agent
            notify_frontend: Frontend identifier for MQTT notifications
            notify_chat: Chat ID for MQTT notifications
            name: Optional session name (will be deduplicated by server)

        Returns:
            The actual session name (after deduplication)

        Raises:
            httpx.HTTPError: If the request fails
        """
        payload: dict[str, Any] = {
            "prompt": prompt,
            "notify_frontend": notify_frontend,
            "notify_chat": notify_chat,
        }
        if name:
            payload["name"] = name

        resp = await self._client.post(
            f"{self.base_url}/sessions",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data["name"])

    async def list_sessions(self) -> list[dict[str, Any]]:
        """List all background sessions.

        Returns:
            A list of session info dicts (without index)

        Raises:
            httpx.HTTPError: If the request fails
        """
        resp = await self._client.get(f"{self.base_url}/sessions")
        resp.raise_for_status()
        data = resp.json()
        return data["sessions"]  # type: ignore[no-any-return]

    async def get_session(self, name: str) -> dict[str, Any]:
        """Get session info by name.

        Args:
            name: Session name

        Returns:
            Session info dict

        Raises:
            httpx.HTTPError: If the request fails (404 if not found)
        """
        resp = await self._client.get(f"{self.base_url}/sessions/{name}")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def send_prompt(self, name: str, prompt: str) -> None:
        """Send a prompt to a session.

        Args:
            name: Session name
            prompt: Prompt text to send

        Raises:
            httpx.HTTPError: If the request fails (404 if not found, 409 if running)
        """
        resp = await self._client.post(
            f"{self.base_url}/sessions/{name}/prompt",
            json={"prompt": prompt},
        )
        resp.raise_for_status()

    async def delete_session(self, name: str) -> None:
        """Delete a session.

        Args:
            name: Session name

        Raises:
            httpx.HTTPError: If the request fails (404 if not found)
        """
        resp = await self._client.delete(f"{self.base_url}/sessions/{name}")
        resp.raise_for_status()

    async def get_history(self, name: str) -> list[dict[str, Any]]:
        """Get session history.

        Args:
            name: Session name

        Returns:
            List of message dicts

        Raises:
            httpx.HTTPError: If the request fails (404 if not found)
        """
        resp = await self._client.get(f"{self.base_url}/sessions/{name}/history")
        resp.raise_for_status()
        data = resp.json()
        return data["messages"]  # type: ignore[no-any-return]

    async def get_last(self, name: str) -> dict | None:
        """Get last agent message.

        Args:
            name: Session name

        Returns:
            Last agent message dict, or None if no agent message exists (204 No Content)

        Raises:
            httpx.HTTPError: If the request fails (404 if not found)
        """
        resp = await self._client.get(f"{self.base_url}/sessions/{name}/last")
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        data = resp.json()
        return dict(data) if data else None
