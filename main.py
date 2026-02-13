"""nochan entry point - starts the WebSocket server."""

import asyncio
import logging
from pathlib import Path

from nochan.config import get_config_path, load_config
from nochan.log import setup_logging
from nochan.opencode import SubprocessOpenCodeBackend
from nochan.prompt import PromptBuilder
from nochan.server import NochanServer
from nochan.session import SessionManager

logger = logging.getLogger("nochan.main")


async def main() -> None:
    """Initialize all modules and start the server."""
    # Load configuration
    config_path = get_config_path()
    config = load_config(config_path)

    # Initialize logging
    setup_logging(config.logging)
    logger.info("nochan starting up (config: %s)", config_path)

    # Ensure opencode work directory exists
    work_dir = Path(config.opencode.work_dir).expanduser()
    work_dir.mkdir(parents=True, exist_ok=True)
    logger.info("OpenCode work directory: %s", work_dir)

    # Initialize session manager
    db_path = Path(config.database.path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    session_manager = SessionManager(str(db_path))
    await session_manager.init()
    logger.info("Session manager initialized (db: %s)", db_path)

    # Initialize OpenCode backend
    opencode_backend = SubprocessOpenCodeBackend(
        command=config.opencode.command,
        work_dir=str(work_dir),
        max_concurrent=config.opencode.max_concurrent,
    )
    logger.info(
        "OpenCode backend ready (max_concurrent: %d)",
        config.opencode.max_concurrent,
    )

    # Initialize prompt builder (prompt dir is relative to opencode work_dir)
    prompt_dir = work_dir / config.prompt.dir
    prompt_builder = PromptBuilder(
        prompt_dir=prompt_dir,
        session_init_file=config.prompt.session_init_file,
        message_prefix_file=config.prompt.message_prefix_file,
    )

    # Start WebSocket server
    server = NochanServer(
        host=config.server.host,
        port=config.server.port,
        session_manager=session_manager,
        opencode_backend=opencode_backend,
        prompt_builder=prompt_builder,
    )

    try:
        await server.start()
    finally:
        await session_manager.close()
        logger.info("nochan shut down.")


if __name__ == "__main__":
    import contextlib

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
