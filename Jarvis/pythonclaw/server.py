"""
Daemon server for Jarvis — multi-channel mode.

Supports Telegram, Discord, and WhatsApp channels, individually or combined.
The web dashboard always runs; channels are started alongside it.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from .core.llm.base import LLMProvider
from .core.persistent_agent import PersistentAgent
from .core.session_store import SessionStore
from .scheduler.cron import CronScheduler
from .session_manager import SessionManager

logger = logging.getLogger(__name__)


async def start_channels(
    provider: LLMProvider,
    channels: list[str],
    fastapi_app=None,
) -> list:
    """Start messaging channels (Telegram, Discord, WhatsApp) as background tasks.

    Parameters
    ----------
    provider    : LLM provider instance
    channels    : list of channel names to start
    fastapi_app : optional FastAPI app instance (required for WhatsApp webhook)

    Returns the list of successfully started bot objects.
    Safe to call during FastAPI startup — failures are logged, not raised.
    """
    store = SessionStore()
    session_manager = SessionManager(agent_factory=lambda sid: None, store=store)

    scheduler = CronScheduler(
        session_manager=session_manager,
    )

    def agent_factory(session_id: str) -> PersistentAgent:
        return PersistentAgent(
            provider=provider,
            store=store,
            session_id=session_id,
            cron_manager=scheduler,
            verbose=False,
        )

    session_manager.set_factory(agent_factory)

    active_bots: list = []

    if "telegram" in channels:
        try:
            from .channels.telegram_bot import create_bot_from_env
            bot = create_bot_from_env(session_manager)
            scheduler._telegram_bot = bot
            await bot.start_async()
            active_bots.append(bot)
            logger.info("[Server] Telegram bot started.")
        except Exception as exc:
            logger.warning("[Server] Telegram channel failed to start: %s", exc)

    if "discord" in channels:
        try:
            from .channels.discord_bot import create_bot_from_env as create_discord
            discord_bot = create_discord(session_manager)
            asyncio.create_task(discord_bot.start_async())
            active_bots.append(discord_bot)
            logger.info("[Server] Discord bot started.")
        except Exception as exc:
            logger.warning("[Server] Discord channel failed to start: %s", exc)

    if "whatsapp" in channels:
        try:
            from .channels.whatsapp_bot import create_bot_from_env as create_whatsapp
            wa_bot = create_whatsapp(session_manager)
            if fastapi_app is not None:
                wa_bot.mount(fastapi_app)
            await wa_bot.start_async()
            active_bots.append(wa_bot)
            logger.info("[Server] WhatsApp channel started.")
        except Exception as exc:
            logger.warning("[Server] WhatsApp channel failed to start: %s", exc)

    if active_bots:
        scheduler.start()
        logger.info("[Server] Channels running: %s", ", ".join(channels))
    else:
        logger.warning("[Server] No channels started — check tokens in pythonclaw.json.")

    return active_bots


async def run_server(
    provider: LLMProvider,
    channels: list[str] | None = None,
) -> None:
    """Standalone server entry point (channels only, no web).

    Kept for backward compatibility.  Prefer using ``start_channels``
    together with the web dashboard in ``_run_foreground``.
    """
    if channels is None:
        channels = ["telegram"]

    active_bots = await start_channels(provider, channels)

    if not active_bots:
        logger.error("[Server] No channels started. Exiting.")
        return

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("[Server] Shutdown signal received.")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, OSError):
            pass

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("[Server] Shutting down...")
        for bot in active_bots:
            if hasattr(bot, 'stop_async'):
                await bot.stop_async()
        logger.info("[Server] Shutdown complete.")
