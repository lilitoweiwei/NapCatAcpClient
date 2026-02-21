"""Unified command system for ncat.

Provides a registry-based command system where each command is defined by:
- A regex pattern for matching
- A handler function that receives parsed groups
- Help text that's automatically aggregated

Usage::

    registry = CommandRegistry(header="My Commands:")

    @registry.register(
        pattern=r"^/hello\\s+(?P<name>\\w+)$",
        help_text="/hello <name> - Say hello",
        name="hello",
    )
    async def handle_hello(name: str, chat_id: str, reply_fn, event):
        await reply_fn(event, f"Hello, {name}!")

    # Execute commands
    matched = await registry.execute(text, chat_id=..., reply_fn=..., event=...)

    # Generate help text
    help_text = registry.generate_help_text()
"""

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger("ncat.command_system")

# Type alias for command handler functions
# Handlers receive matched groups as kwargs plus context (chat_id, reply_fn, event, etc.)
CommandHandler = Callable[..., Awaitable[None]]


@dataclass
class CommandSpec:
    """Specification for a single command."""

    pattern: str  # Regex pattern for matching
    handler: CommandHandler  # Async function to execute the command
    help_text: str  # Help text shown in /help
    name: str  # Command name for logging


class CommandRegistry:
    """Registry for command handlers with automatic help text generation.

    Attributes:
        header_text: Optional header text shown before all commands in help.
    """

    def __init__(self, header_text: str = ""):
        """Initialize command registry.

        Args:
            header_text: Header text shown before all commands in help.
        """
        self._commands: list[CommandSpec] = []
        self._header_text = header_text
        self._dependencies: dict = {}

    def set_dependency(self, key: str, value) -> None:
        """Set a dependency that will be injected into command handlers.

        Args:
            key: Dependency name (will be passed as kwarg to handlers)
            value: Dependency value
        """
        self._dependencies[key] = value

    def register(
        self, pattern: str, help_text: str, name: str
    ) -> Callable[[CommandHandler], CommandHandler]:
        """Decorator to register a command handler.

        Args:
            pattern: Regex pattern for matching command text
            help_text: Help text shown in /help
            name: Command name for logging

        Returns:
            Decorator function

        Example:
            @registry.register(
                pattern=r"^/test$",
                help_text="/test - Test command",
                name="test",
            )
            async def handle_test(chat_id: str, reply_fn, event):
                await reply_fn(event, "Test!")
        """

        def decorator(func: CommandHandler) -> CommandHandler:
            self._commands.append(
                CommandSpec(
                    pattern=pattern,
                    handler=func,
                    help_text=help_text,
                    name=name,
                )
            )
            logger.debug("Registered command: %s (pattern: %s)", name, pattern)
            return func

        return decorator

    def generate_help_text(self) -> str:
        """Generate aggregated help text from all registered commands.

        Returns:
            Formatted help text with header and all command help strings
        """
        lines = []
        if self._header_text:
            lines.append(self._header_text)

        for cmd in self._commands:
            lines.append(f"  {cmd.help_text}")

        return "\n".join(lines)

    async def execute(self, text: str, **context) -> bool:
        """Try to execute a command from text.

        Args:
            text: Message text to parse (e.g., "/bg new test prompt")
            **context: Context variables to pass to handlers (chat_id, reply_fn, event, etc.)

        Returns:
            True if a command was matched and executed, False otherwise
        """
        for cmd in self._commands:
            match = re.match(cmd.pattern, text)
            if match:
                logger.info("Executing command: %s", cmd.name)

                # Merge matched groups with context dependencies
                kwargs = {**match.groupdict(), **self._dependencies, **context}

                try:
                    await cmd.handler(**kwargs)
                    return True
                except Exception as e:
                    logger.exception("Error executing command %s: %s", cmd.name, e)
                    # Don't return False here - let the caller handle error messages
                    raise

        return False

    def get_command_count(self) -> int:
        """Get the number of registered commands."""
        return len(self._commands)
