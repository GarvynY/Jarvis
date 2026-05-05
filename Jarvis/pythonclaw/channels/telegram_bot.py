"""
Telegram channel for pythonclaw.

Telegram is purely a *channel* — it handles sending and receiving messages.
Session lifecycle (which Agent handles which chat) is delegated to the
SessionManager, which is shared across all channels and the cron scheduler.

Session IDs used by this channel: "telegram:{chat_id}"

Commands
--------
  /start          — greeting + usage hint
  /reset          — discard and recreate the current session
  /status         — show session info (provider, skills, memory, tokens, compactions)
  /compact [hint] — compact conversation history
  /my_profile     — show structured personalization data
  /feedback       — record lightweight alert feedback
  /privacy        — show Phase 8 privacy design
  /delete_profile — delete structured personalization data
  <text>          — forwarded to Agent.chat(), reply sent back
  <photo>         — image sent to LLM with optional caption

Access control
--------------
Set TELEGRAM_ALLOWED_USERS to a comma-separated list of integer Telegram user
IDs to restrict access.  Leave empty (or unset) to allow all users.

Group behaviour
---------------
Set ``channels.telegram.requireMention`` to ``true`` in pythonclaw.json to
require @bot mention in group chats.  DMs always respond.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import logging
import math
import queue as _queue
import re
import statistics
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .. import config
from ._telegram_helpers import (
    # Path constants
    _SKILL_DIR,
    _RESEARCH_DIR,
    # String constants / UI text
    _WELCOME_GUIDE,
    _RETURNING_WELCOME,
    _PRIVACY_TEXT,
    _UPDATE_PROFILE_USAGE,
    _FEEDBACK_USAGE,
    # Session / wizard state keys
    _UPDATE_PROFILE_WIZARD_KEY,
    _DELETE_PROFILE_PENDING_KEY,
    _UPDATE_PROFILE_WIZARD_STEPS,
    _ONBOARDING_KEY,
    _ONBOARDING_STEPS,
    _ONBOARDING_INTRO,
    # Feedback inline buttons
    _FB_PREFIX,
    _FB_SOURCE_LABELS,
    _FEEDBACK_TYPE_ALIASES,
    _FEEDBACK_TYPE_LABELS,
    # Field alias lookup
    _PROFILE_FIELD_ALIASES,
    # Greeting helpers
    _is_new_user,
    _mark_greeted,
    _get_recent_news_text,
    _load_cnyaud,
    # Rate / bank helpers
    _preferred_bank_names_for_user,
    _format_bank_rate_table,
    # Profile display
    _format_user_profile,
    _delete_profile_requires_confirmation,
    # Feedback UI
    _make_feedback_keyboard,
    _parse_feedback_args,
    _format_feedback_confirmation,
    # Profile update / onboarding
    _parse_update_profile_args,
    _parse_update_profile_value,
    _format_update_profile_error,
    _update_profile_prompt,
    _onboarding_prompt,
    _format_update_profile_confirmation,
    # Research brief
    _ensure_research_path,
    _format_research_brief,
    # Message utilities
    _clean_response,
    _split_message,
)

if TYPE_CHECKING:
    from ..session_manager import SessionManager

logger = logging.getLogger(__name__)

class TelegramBot:
    """
    Telegram channel — pure I/O layer.

    Receives messages from Telegram and routes them to the appropriate Agent
    via the shared SessionManager.  Does not own or manage Agent instances.
    """

    def __init__(
        self,
        session_manager: "SessionManager",
        token: str,
        allowed_users: list[int] | None = None,
        require_mention: bool = False,
    ) -> None:
        self._sm = session_manager
        self._token = token
        self._allowed_users: set[int] = set(allowed_users) if allowed_users else set()
        self._require_mention = require_mention
        self._app: Application | None = None
        self._bot_username: str | None = None

    # ── Session ID convention ─────────────────────────────────────────────────

    @staticmethod
    def _session_id(chat_id: int) -> str:
        return f"telegram:{chat_id}"

    # ── Push message (called by cron / heartbeat) ─────────────────────────────

    async def send_message(
        self, chat_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        """Send a message to a specific chat (used by cron/heartbeat)."""
        if self._app is None:
            logger.warning("[Telegram] send_message called before bot is running")
            return
        await self._app.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=parse_mode
        )

    # ── Access control ────────────────────────────────────────────────────────

    def _is_allowed(self, user_id: int) -> bool:
        if not self._allowed_users:
            return True
        return user_id in self._allowed_users

    async def _check_access(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        user = update.effective_user
        if update.message is None:
            logger.warning("[Telegram] Ignored update without message")
            return False
        if user is None or not self._is_allowed(user.id):
            logger.warning("[Telegram] Rejected user_id=%s", user.id if user else "unknown")
            await update.message.reply_text("Sorry, you are not authorised to use this bot.")
            return False
        return True

    def _is_group(self, update: Update) -> bool:
        """Return True if the message is from a group/supergroup."""
        return update.effective_chat.type in ("group", "supergroup")

    def _is_mentioned(self, update: Update) -> bool:
        """Check if the bot is @mentioned in the message text."""
        text = update.message.text or update.message.caption or ""
        if self._bot_username and f"@{self._bot_username}" in text:
            return True
        entities = update.message.entities or update.message.caption_entities or []
        for ent in entities:
            if ent.type == "mention" and self._bot_username:
                mention = text[ent.offset:ent.offset + ent.length]
                if mention.lower() == f"@{self._bot_username.lower()}":
                    return True
        return False

    def _strip_mention(self, text: str) -> str:
        """Remove the @bot mention from message text."""
        if self._bot_username:
            text = text.replace(f"@{self._bot_username}", "").strip()
        return text

    # ── Command handlers ──────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update, context):
            return
        chat_id = update.effective_chat.id
        sid = self._session_id(chat_id)
        self._sm.get_or_create(sid)

        is_new = _is_new_user(chat_id)
        if is_new:
            _mark_greeted(chat_id)
            await update.message.reply_text(_WELCOME_GUIDE)
            recent = _get_recent_news_text()
            if recent:
                await update.message.reply_text(recent)
        else:
            await update.message.reply_text(_RETURNING_WELCOME)
        await self._maybe_start_onboarding(update, context)

    async def _cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update, context):
            return
        sid = self._session_id(update.effective_chat.id)
        self._sm.reset(sid)
        await update.message.reply_text("当前对话已重置。你可以重新发送问题，Jarvis 会从新的会话开始处理。")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update, context):
            return
        sid = self._session_id(update.effective_chat.id)
        agent = self._sm.get_or_create(sid)
        from ..core.compaction import estimate_tokens
        await update.message.reply_text(
            f"\U0001f4ca Session Status\n"
            f"  Session ID   : {sid}\n"
            f"  Provider     : {type(agent.provider).__name__}\n"
            f"  Skills       : {len(agent.loaded_skill_names)} loaded\n"
            f"  Memories     : {len(agent.memory.list_all())} entries\n"
            f"  History      : {len(agent.messages)} messages\n"
            f"  Est. tokens  : ~{estimate_tokens(agent.messages):,}\n"
            f"  Compactions  : {agent.compaction_count}\n"
            f"  Total sessions: {len(self._sm)}"
        )

    async def _cmd_compact(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update, context):
            return
        sid = self._session_id(update.effective_chat.id)
        agent = self._sm.get_or_create(sid)
        hint: str | None = " ".join(context.args).strip() or None if context.args else None
        await update.message.reply_text("\u23f3 Compacting conversation history...")
        try:
            result = agent.compact(instruction=hint)
        except Exception as exc:
            result = f"Compaction failed: {exc}"
        for chunk in _split_message(result):
            await update.message.reply_text(chunk)

    async def _cmd_clear_files(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update, context):
            return
        from .. import config as _cfg
        count = _cfg.clear_files()
        await update.message.reply_text(f"Cleared {count} file(s) from the downloads folder.")

    async def _cmd_my_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update, context):
            return
        from ..core.personalization import get_or_create_user, get_user_profile

        if update.effective_user is None or update.message is None:
            return
        telegram_user_id = update.effective_user.id
        profile = get_user_profile(telegram_user_id)
        created = not bool(profile)
        if created:
            get_or_create_user(telegram_user_id)
            profile = get_user_profile(telegram_user_id)

        await update.message.reply_text(_format_user_profile(profile, created=created))

    async def _cmd_privacy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update, context):
            return
        await update.message.reply_text(_PRIVACY_TEXT)

    async def _record_feedback(
        self,
        update: Update,
        args: list[str],
    ) -> None:
        if update.effective_user is None or update.message is None:
            return

        try:
            event_type, topic = _parse_feedback_args(args)
        except ValueError:
            await update.message.reply_text(_FEEDBACK_USAGE)
            return

        from ..core.personalization import log_feedback_event

        try:
            event_id = log_feedback_event(
                update.effective_user.id,
                event_type,
                topic=topic,
                message_id=str(update.message.message_id),
                metadata={"source": "telegram_command"},
            )
        except ValueError as exc:
            if str(exc).startswith("topic_not_allowed:"):
                from ..core.personalization import ALLOWED_FEEDBACK_TOPICS
                valid = "、".join(sorted(ALLOWED_FEEDBACK_TOPICS))
                await update.message.reply_text(
                    "主题不在允许列表中，请使用以下主题之一：\n"
                    f"{valid}\n\n"
                    "示例：/feedback not_interested topic=通用市场新闻"
                )
            else:
                await update.message.reply_text("反馈内容不适合保存，请简化后再试。")
            return
        except Exception:
            logger.exception(
                "[Telegram] Failed to log feedback for user_id=%s",
                update.effective_user.id,
            )
            await update.message.reply_text("记录反馈失败，请稍后再试。")
            return

        logger.info(
            "[Telegram] Logged feedback event_id=%s user_id=%s event_type=%s topic=%s",
            event_id,
            update.effective_user.id,
            event_type,
            topic or "",
        )
        await update.message.reply_text(_format_feedback_confirmation(event_type, topic))

        # Run the rule-based aggregator after each feedback event.
        # This is a lightweight SQL-only operation — no LLM is involved.
        try:
            from ..core.personalization import update_inferred_preferences_from_feedback
            update_inferred_preferences_from_feedback(update.effective_user.id)
        except Exception:
            logger.warning(
                "[Telegram] Inferred preference aggregation failed for user_id=%s",
                update.effective_user.id,
                exc_info=True,
            )

    async def _cmd_feedback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update, context):
            return
        await self._record_feedback(update, list(context.args or []))

    async def _handle_feedback_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle inline button feedback (👍 / 👎 / 🚫) from any push message."""
        query = update.callback_query
        if query is None or query.from_user is None:
            return

        data = query.data or ""
        if not data.startswith(_FB_PREFIX):
            return

        # Parse "fb:useful:research" → event_type="useful", source="research"
        tail = data[len(_FB_PREFIX):]
        parts = tail.split(":", 1)
        event_type = parts[0]
        source = parts[1] if len(parts) > 1 else "unknown"

        if event_type not in _FEEDBACK_TYPE_LABELS:
            await query.answer("未知反馈类型。")
            return

        from ..core.personalization import log_feedback_event, update_inferred_preferences_from_feedback

        try:
            log_feedback_event(
                query.from_user.id,
                event_type,
                topic=None,   # source goes into metadata; avoids topic whitelist
                message_id=str(query.message.message_id) if query.message else None,
                metadata={"source": f"inline_button:{source}"},
            )
        except Exception:
            logger.exception(
                "[Telegram] Failed to log inline feedback user_id=%s", query.from_user.id
            )
            await query.answer("记录失败，请稍后再试。")
            return

        # Remove buttons so the same message can't be rated twice
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        label = _FEEDBACK_TYPE_LABELS[event_type]
        source_label = _FB_SOURCE_LABELS.get(source, source)
        await query.answer(f"已记录：{label}（{source_label}），谢谢！")

        logger.info(
            "[Telegram] Inline feedback logged: user_id=%s event=%s source=%s msg_id=%s",
            query.from_user.id, event_type, source,
            query.message.message_id if query.message else "?",
        )

        try:
            update_inferred_preferences_from_feedback(query.from_user.id)
        except Exception:
            logger.warning(
                "[Telegram] Inferred preference aggregation failed for user_id=%s",
                query.from_user.id, exc_info=True,
            )

    async def _maybe_start_onboarding(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> bool:
        """Start lightweight Phase 8 onboarding for private chats only."""
        if update.effective_user is None or update.message is None:
            return False
        if self._is_group(update):
            return False

        from ..core.personalization import get_or_create_user, get_user_profile

        try:
            telegram_user_id = update.effective_user.id
            profile = get_user_profile(telegram_user_id)
            if not profile:
                get_or_create_user(telegram_user_id)
                profile = get_user_profile(telegram_user_id)
        except Exception:
            logger.exception(
                "[Telegram] Failed to load onboarding state for user_id=%s",
                update.effective_user.id,
            )
            return False

        user = profile.get("user") or {}
        if bool(user.get("onboarding_completed")):
            return False

        context.user_data.pop(_UPDATE_PROFILE_WIZARD_KEY, None)
        context.user_data[_ONBOARDING_KEY] = {
            "step": 0,
            "updates": {},
        }
        await update.message.reply_text(f"{_ONBOARDING_INTRO}\n\n{_onboarding_prompt(0)}")
        return True

    async def _cmd_update_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update, context):
            return
        await self._handle_update_profile_command(update, context, list(context.args or []))

    async def _handle_update_profile_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: list[str],
    ) -> None:
        if update.effective_user is None or update.message is None:
            return

        context.user_data.pop(_ONBOARDING_KEY, None)
        context.user_data.pop(_DELETE_PROFILE_PENDING_KEY, None)
        if not args:
            context.user_data[_UPDATE_PROFILE_WIZARD_KEY] = {
                "step": 0,
                "updates": {},
            }
            await update.message.reply_text(
                "我会逐步帮你更新明确偏好。你可以随时输入“跳过”或“取消”。\n\n"
                f"{_update_profile_prompt(0)}"
            )
            return

        try:
            updates = _parse_update_profile_args(args)
        except ValueError as exc:
            await update.message.reply_text(_format_update_profile_error(str(exc)))
            return

        from ..core.personalization import update_explicit_preferences

        try:
            update_explicit_preferences(update.effective_user.id, updates)
        except ValueError:
            await update.message.reply_text(
                "输入包含不支持或不适合保存到个性化资料的内容。\n\n"
                f"{_UPDATE_PROFILE_USAGE}"
            )
            return
        except Exception:
            logger.exception(
                "[Telegram] Failed to update personalization profile for user_id=%s",
                update.effective_user.id,
            )
            await update.message.reply_text("更新个性化偏好失败，请稍后再试。")
            return

        await update.message.reply_text(_format_update_profile_confirmation(updates))

    async def _handle_update_profile_wizard(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_text: str,
    ) -> bool:
        wizard = context.user_data.get(_UPDATE_PROFILE_WIZARD_KEY)
        if not wizard:
            return False
        if update.effective_user is None or update.message is None:
            return True

        text = user_text.strip()
        if text in {"取消", "/cancel"}:
            context.user_data.pop(_UPDATE_PROFILE_WIZARD_KEY, None)
            await update.message.reply_text("已取消修改个性化偏好。")
            return True
        if not text:
            step = int(wizard.get("step", 0))
            await update.message.reply_text(
                "请发送文字内容，或输入“跳过”“取消”。\n\n"
                f"{_update_profile_prompt(step)}"
            )
            return True

        step = int(wizard.get("step", 0))
        updates = dict(wizard.get("updates") or {})
        field, _ = _UPDATE_PROFILE_WIZARD_STEPS[step]

        if text != "跳过":
            try:
                updates[field] = _parse_update_profile_value(field, text)
            except ValueError as exc:
                await update.message.reply_text(
                    f"{_UPDATE_PROFILE_ERROR_MESSAGES.get(str(exc), '输入不正确，请重新输入。')}\n\n"
                    f"{_update_profile_prompt(step)}"
                )
                return True

        step += 1
        if step < len(_UPDATE_PROFILE_WIZARD_STEPS):
            context.user_data[_UPDATE_PROFILE_WIZARD_KEY] = {
                "step": step,
                "updates": updates,
            }
            await update.message.reply_text(_update_profile_prompt(step))
            return True

        context.user_data.pop(_UPDATE_PROFILE_WIZARD_KEY, None)
        if not updates:
            await update.message.reply_text("没有更新任何偏好。")
            return True

        from ..core.personalization import update_explicit_preferences

        try:
            update_explicit_preferences(update.effective_user.id, updates)
        except ValueError:
            await update.message.reply_text("输入包含不适合保存到个性化资料的内容，已取消本次更新。")
            return True
        except Exception:
            logger.exception(
                "[Telegram] Failed to update personalization profile for user_id=%s",
                update.effective_user.id,
            )
            await update.message.reply_text("更新个性化偏好失败，请稍后再试。")
            return True

        await update.message.reply_text(_format_update_profile_confirmation(updates))
        return True

    async def _handle_onboarding(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_text: str,
    ) -> bool:
        state = context.user_data.get(_ONBOARDING_KEY)
        if not state:
            return False
        if update.effective_user is None or update.message is None:
            return True

        from ..core.personalization import (
            get_user_profile,
            mark_onboarding_completed,
            update_explicit_preferences,
        )

        async def finish(updates: dict[str, Any]) -> None:
            telegram_user_id = update.effective_user.id
            try:
                if updates:
                    update_explicit_preferences(telegram_user_id, updates)
                mark_onboarding_completed(telegram_user_id)
                profile = get_user_profile(telegram_user_id)
            except ValueError:
                context.user_data.pop(_ONBOARDING_KEY, None)
                await update.message.reply_text("输入包含不适合保存到个性化资料的内容，已结束引导。")
                return
            except Exception:
                logger.exception(
                    "[Telegram] Failed to complete onboarding for user_id=%s",
                    telegram_user_id,
                )
                await update.message.reply_text("保存入门设置失败，请稍后再试。")
                return

            context.user_data.pop(_ONBOARDING_KEY, None)
            await update.message.reply_text(
                "入门设置已完成。下面是你当前的个性化资料：\n\n"
                f"{_format_user_profile(profile)}"
            )

        text = user_text.strip()
        if text in {"跳过引导", "跳过全部", "取消引导"}:
            await finish({})
            return True
        if not text:
            step = int(state.get("step", 0))
            await update.message.reply_text(
                "请发送文字内容，或回复“跳过”“跳过引导”。\n\n"
                f"{_onboarding_prompt(step)}"
            )
            return True

        step = int(state.get("step", 0))
        updates = dict(state.get("updates") or {})
        field, _ = _ONBOARDING_STEPS[step]

        if text != "跳过":
            try:
                updates[field] = _parse_update_profile_value(field, text)
            except ValueError as exc:
                await update.message.reply_text(
                    f"{_UPDATE_PROFILE_ERROR_MESSAGES.get(str(exc), '输入不正确，请重新输入。')}\n\n"
                    f"{_onboarding_prompt(step)}"
                )
                return True

        step += 1
        if step < len(_ONBOARDING_STEPS):
            context.user_data[_ONBOARDING_KEY] = {
                "step": step,
                "updates": updates,
            }
            await update.message.reply_text(_onboarding_prompt(step))
            return True

        await finish(updates)
        return True

    async def _delete_profile_for_current_user(self, update: Update) -> None:
        if update.effective_user is None or update.message is None:
            return

        from ..core.personalization import delete_user_profile, get_user_profile

        telegram_user_id = update.effective_user.id
        try:
            existing = get_user_profile(telegram_user_id)
            if not existing:
                await update.message.reply_text("目前没有找到你的个性化资料，无需删除。")
                return
            deleted = delete_user_profile(telegram_user_id)
        except Exception:
            logger.exception(
                "[Telegram] Failed to delete personalization profile for user_id=%s",
                telegram_user_id,
            )
            await update.message.reply_text("删除个性化数据失败，请稍后再试。")
            return

        if deleted:
            logger.info(
                "[Telegram] Deleted personalization profile for user_id=%s",
                telegram_user_id,
            )
            await update.message.reply_text(
                "你的结构化个性化数据已删除，包括偏好、反馈记录和短期 raw events。"
                "对话历史和系统运行日志不会因此删除。之后 Jarvis 将按默认设置为你服务。"
            )
        else:
            await update.message.reply_text("目前没有找到你的个性化资料，无需删除。")

    async def _handle_delete_profile_confirmation(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_text: str,
    ) -> bool:
        if not context.user_data.get(_DELETE_PROFILE_PENDING_KEY):
            return False
        if update.effective_user is None or update.message is None:
            return True

        text = user_text.strip()
        if text in {"确定", "确认"}:
            context.user_data.pop(_DELETE_PROFILE_PENDING_KEY, None)
            await self._delete_profile_for_current_user(update)
            return True
        if text in {"取消", "/cancel"}:
            context.user_data.pop(_DELETE_PROFILE_PENDING_KEY, None)
            await update.message.reply_text("已取消删除个性化数据。")
            return True
        return False

    async def _handle_text_command_alias(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_text: str,
    ) -> bool:
        """Handle Chinese slash-like commands that Telegram cannot register."""
        text = user_text.strip()
        if not text.startswith("/"):
            return False
        command, _, rest = text.partition(" ")
        args = rest.split() if rest else []

        if command in {"/我的资料", "/资料"}:
            await self._cmd_my_profile(update, context)
            return True
        if command in {"/隐私", "/隐私说明"}:
            await self._cmd_privacy(update, context)
            return True
        if command in {"/银行牌价", "/银行汇率", "/银行报价", "/十大银行"}:
            if not await self._check_access(update, context):
                return True
            await self._shortcut_bank_rates(update, show_all=command == "/十大银行")
            return True
        if command in {"/修改资料", "/更新资料", "/更新偏好"}:
            if not await self._check_access(update, context):
                return True
            await self._handle_update_profile_command(update, context, args)
            return True
        if command in {"/删除资料", "/删除个性化"}:
            if not await self._check_access(update, context):
                return True
            await self._handle_delete_profile_command(update, context, args)
            return True
        if command in {"/反馈"}:
            if not await self._check_access(update, context):
                return True
            await self._record_feedback(update, args)
            return True
        if command in {"/清空推断", "/清除推断"}:
            if not await self._check_access(update, context):
                return True
            await self._cmd_clear_inferred(update, context)
            return True
        return False

    async def _cmd_delete_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update, context):
            return
        await self._handle_delete_profile_command(update, context, list(context.args or []))

    async def _cmd_clear_inferred(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Clear only inferred_preferences, keeping explicit prefs and feedback history."""
        if not await self._check_access(update, context):
            return
        if update.effective_user is None or update.message is None:
            return

        from ..core.personalization import clear_inferred_preferences

        telegram_user_id = update.effective_user.id
        try:
            deleted = clear_inferred_preferences(telegram_user_id)
        except Exception:
            logger.exception(
                "[Telegram] Failed to clear inferred preferences for user_id=%s",
                telegram_user_id,
            )
            await update.message.reply_text("清空推断偏好失败，请稍后再试。")
            return

        if deleted:
            await update.message.reply_text(
                "已清空 Jarvis 的推断偏好。\n\n"
                "你的明确偏好和反馈记录已保留。\n"
                "继续使用 /feedback 后，Jarvis 会从零重新推断。"
            )
        else:
            await update.message.reply_text("目前没有推断偏好记录，无需清空。")

    async def _handle_delete_profile_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: list[str],
    ) -> None:
        if update.effective_user is None or update.message is None:
            return

        context.user_data.pop(_ONBOARDING_KEY, None)
        context.user_data.pop(_UPDATE_PROFILE_WIZARD_KEY, None)
        confirm = bool(args and args[0].lower() in {"确认", "confirm"})
        if _delete_profile_requires_confirmation():
            if not confirm:
                from ..core.personalization import get_user_profile

                try:
                    existing = get_user_profile(update.effective_user.id)
                except Exception:
                    logger.exception(
                        "[Telegram] Failed to load profile before delete confirmation for user_id=%s",
                        update.effective_user.id,
                    )
                    await update.message.reply_text("读取个性化资料失败，请稍后再试。")
                    return
                if not existing:
                    await update.message.reply_text("目前没有找到你的个性化资料，无需删除。")
                    return
                context.user_data[_DELETE_PROFILE_PENDING_KEY] = True
                await update.message.reply_text(
                    "此操作会删除你的结构化个性化数据，包括明确偏好、推断偏好、反馈事件和短期 raw events。"
                    "不会删除对话历史或系统运行日志。\n\n"
                    "如确认删除，请直接回复：确定\n"
                    "如放弃删除，请回复：取消"
                )
                return

        context.user_data.pop(_DELETE_PROFILE_PENDING_KEY, None)
        await self._delete_profile_for_current_user(update)

    # ── Shortcut command handlers (no LLM) ───────────────────────────────────

    async def _shortcut_latest_news(self, update: Update) -> None:
        """Fetch latest news directly without LLM and send formatted result."""
        await update.message.reply_text("📡 正在拉取最新新闻，请稍候…")
        try:
            nm = _load_cnyaud("news_monitor")
            result = nm.check_news()
            text = nm._format_text(result)
        except Exception as exc:
            logger.exception("[Telegram] 最新新闻 fetch failed")
            text = f"⚠️ 获取新闻失败: {exc}"
        for chunk in _split_message(text):
            await update.message.reply_text(chunk)

    async def _shortcut_latest_rate(self, update: Update) -> None:
        """Fetch current CNY/AUD rate directly without LLM and send formatted result."""
        await update.message.reply_text("📡 正在获取最新汇率，请稍候…")
        try:
            fr = _load_cnyaud("fetch_rate")
            data = fr.fetch_rate()
            preferred = _preferred_bank_names_for_user(
                update.effective_user.id if update.effective_user else None
            )
            text = _format_bank_rate_table(
                data,
                getattr(fr, "BANK_SOURCES", None),
                preferred_banks=preferred,
            )
        except Exception as exc:
            logger.exception("[Telegram] 最新汇率 fetch failed")
            text = f"⚠️ 获取汇率失败: {exc}"
        for chunk in _split_message(text):
            await update.message.reply_text(chunk)

    async def _cmd_bank_rates(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update, context):
            return
        args = {str(arg).lower() for arg in (context.args or [])}
        show_all = bool(args & {"all", "全部", "十大银行", "10"})
        await self._shortcut_bank_rates(update, show_all=show_all)

    async def _shortcut_bank_rates(self, update: Update, *, show_all: bool = False) -> None:
        """Fetch AUD quotes from the ten-bank board directly, bypassing LLM."""
        notice = "十大银行" if show_all else "关注银行"
        await update.message.reply_text(f"🏦 正在拉取{notice} AUD 牌价，请稍候…")
        try:
            fr = _load_cnyaud("fetch_rate")
            data = fr.fetch_rate("7d")
            preferred = _preferred_bank_names_for_user(
                update.effective_user.id if update.effective_user else None
            )
            text = _format_bank_rate_table(
                data,
                getattr(fr, "BANK_SOURCES", None),
                preferred_banks=preferred,
                show_all=show_all,
            )
        except Exception as exc:
            logger.exception("[Telegram] 银行牌价 fetch failed")
            text = f"⚠️ 获取银行牌价失败: {exc}"
        for chunk in _split_message(text):
            await update.message.reply_text(chunk)

    async def _shortcut_rate_chart(self, update: Update) -> None:
        """Generate and send a 2-day CNY/AUD chart image without LLM."""
        notice = await update.message.reply_text("📊 正在生成近2日汇率走势图，请稍候…")
        try:
            rc = _load_cnyaud("rate_chart")
            png_bytes = rc.generate_2day_chart()
            await self._app.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=io.BytesIO(png_bytes),
                caption="📈 CNY/AUD 近2日汇率走势（1 AUD = ? CNY）",
            )
            try:
                await notice.delete()
            except Exception:
                pass
        except Exception as exc:
            logger.exception("[Telegram] 汇率波动 chart failed")
            await notice.edit_text(f"⚠️ 生成图表失败: {exc}")

    # ── /fx_research — Phase 9 preset-driven research workflow ───────────────

    async def _cmd_fx_research(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        /fx_research — run the Phase 9 multi-agent research workflow and
        deliver a ResearchBrief as a Telegram message.

        Flow:
          coordinator.run_research("fx_cnyaud", user_id)
              → (ResearchTask, [AgentOutput], CostEstimate)
          SupervisorReportWriter.run(task, preset, outputs, cost)
              → ResearchBrief
          _format_research_brief(brief, latency_s)
              → plain-text Telegram message(s)

        Errors:  if the whole workflow fails, a friendly error is sent.
        Partial: if some agents failed, the brief is still sent with
                 data_gaps populated — no message is suppressed.
        """
        if not await self._check_access(update, context):
            return
        if update.effective_user is None or update.message is None:
            return

        user_id  = update.effective_user.id
        chat_id  = update.effective_chat.id

        notice = await update.message.reply_text(
            "⏳ 正在生成 CNY/AUD 研究简报，请稍候（约 10–30 秒）…"
        )
        typing_task = asyncio.create_task(self._keep_typing(chat_id))
        t0 = time.monotonic()

        try:
            _ensure_research_path()
            import importlib as _importlib  # noqa: PLC0415
            _coord  = _importlib.import_module("coordinator")
            _super  = _importlib.import_module("supervisor")
            _schema = _importlib.import_module("schema")

            run_research          = _coord.run_research
            SupervisorWriter      = _super.SupervisorReportWriter
            PRESET_REGISTRY       = _schema.PRESET_REGISTRY

            logger.info(
                "[Telegram] /fx_research user_id=%s — calling run_research", user_id
            )

            task, outputs, cost_estimate = await run_research(
                preset_name="fx_cnyaud",
                user_id=user_id,
            )

            logger.info(
                "[Telegram] /fx_research task_id=%s — phase-1 done, agent_statuses=%s",
                task.task_id[:8],
                {o.agent_name: o.status for o in outputs},
            )

            preset = PRESET_REGISTRY.get(task.preset_name)
            if preset is None:
                raise ValueError(f"Preset {task.preset_name!r} not found in registry")

            brief = await SupervisorWriter().run(task, preset, outputs, cost_estimate)

            latency_s = time.monotonic() - t0
            logger.info(
                "[Telegram] /fx_research task_id=%s — brief done latency=%.1fs "
                "sections=%d data_gaps=%r",
                task.task_id[:8],
                latency_s,
                len(brief.sections),
                bool(brief.data_gaps),
            )

            text = _format_research_brief(brief, latency_s)

            try:
                await notice.delete()
            except Exception:
                pass

            chunks = _split_message(text)
            for i, chunk in enumerate(chunks):
                kb = _make_feedback_keyboard("research") if i == len(chunks) - 1 else None
                await update.message.reply_text(chunk, reply_markup=kb)

        except Exception as exc:
            latency_s = time.monotonic() - t0
            logger.exception(
                "[Telegram] /fx_research failed user_id=%s latency=%.1fs",
                user_id, latency_s,
            )
            try:
                await notice.edit_text(
                    "⚠️ 研究简报生成失败，请稍后再试。\n"
                    f"（错误类型：{type(exc).__name__}）\n\n"
                    "可能原因：模型服务、数据源、搜索配置或本地缓存问题。"
                )
            except Exception:
                await update.message.reply_text("⚠️ 研究简报生成失败，请稍后再试。")
        finally:
            typing_task.cancel()

    # ── Message handler (text + photos) ───────────────────────────────────────

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update, context):
            return

        if self._is_group(update) and self._require_mention:
            if not self._is_mentioned(update):
                return

        user_text = (update.message.text or update.message.caption or "").strip()
        user_text = self._strip_mention(user_text)

        has_photo = bool(update.message.photo)
        has_voice = bool(update.message.voice or update.message.audio)

        if has_voice:
            transcript = await self._transcribe_voice(update)
            if transcript is None:
                return
            user_text = transcript

        if not user_text and not has_photo:
            return

        if await self._handle_text_command_alias(update, context, user_text):
            return
        if await self._handle_update_profile_wizard(update, context, user_text):
            return
        if await self._handle_delete_profile_confirmation(update, context, user_text):
            return

        # ── Shortcut commands (bypass onboarding and LLM entirely) ───────────
        _cmd = user_text.strip()
        if _cmd == "最新新闻":
            await self._shortcut_latest_news(update)
            return
        if _cmd == "最新汇率":
            await self._shortcut_latest_rate(update)
            return
        if _cmd in {"银行牌价", "银行汇率", "银行报价"}:
            await self._shortcut_bank_rates(update)
            return
        if _cmd in {"十大银行", "10大银行"}:
            await self._shortcut_bank_rates(update, show_all=True)
            return
        if _cmd == "汇率波动":
            await self._shortcut_rate_chart(update)
            return

        if await self._handle_onboarding(update, context, user_text):
            return

        # ── First-time welcome (fires once per user, no LLM) ──────────────────
        _cid = update.effective_chat.id
        if _is_new_user(_cid):
            _mark_greeted(_cid)
            await update.message.reply_text(_WELCOME_GUIDE)
            _recent = _get_recent_news_text()
            if _recent:
                await update.message.reply_text(_recent)

        if await self._maybe_start_onboarding(update, context):
            return

        sid = self._session_id(update.effective_chat.id)
        agent = self._sm.get_or_create(sid)

        if self._sm.is_locked(sid):
            await update.message.reply_text("\u23f3 Processing previous message\u2026")

        try:
            await update.message.set_reaction([ReactionTypeEmoji("\U0001f440")])
        except Exception:
            pass

        chat_input = user_text or ""
        if has_photo:
            chat_input = await self._build_image_input(
                update, user_text or "What's in this image?"
            )

        token_queue: _queue.Queue[str] = _queue.Queue()

        typing_task = asyncio.create_task(
            self._keep_typing(update.message.chat_id)
        )
        try:
            async with self._sm.acquire(sid):
                loop = asyncio.get_event_loop()
                chat_id = update.effective_chat.id
                self._register_file_sender(loop, chat_id)
                future = loop.run_in_executor(
                    None, agent.chat_stream, chat_input, token_queue.put,
                )
                await self._flush_stream(update, token_queue, future)
        except Exception as exc:
            logger.exception("[Telegram] Agent error")
            await update.message.reply_text(f"Sorry, something went wrong: {exc}")
        finally:
            typing_task.cancel()

        try:
            await update.message.set_reaction([])
        except Exception:
            pass

    _AGENT_TIMEOUT = 600

    async def _flush_stream(
        self,
        update: Update,
        token_queue: "_queue.Queue[str]",
        future: "asyncio.Future[str]",
    ) -> None:
        """Collect streamed tokens and deliver as 2-3 large messages.

        Strategy: accumulate all tokens silently. Tool-call markers are
        stripped but do NOT trigger new messages.  Content is edit-in-place
        updated into a single live message; only when a message hits the
        Telegram 4096 char limit is a new message started.

        No heartbeat / "still working" messages are sent.
        """
        buf: list[str] = []
        live_msg = None
        live_text = ""
        sent_any = False
        THROTTLE = 2.0
        last_edit = time.monotonic()
        start_time = time.monotonic()
        _MARKER = re.compile(r'`\[calling:\s*([^\]]+)\]`')

        while not future.done():
            if (time.monotonic() - start_time) > self._AGENT_TIMEOUT:
                logger.warning(
                    "[Telegram] Agent timeout after %ds", self._AGENT_TIMEOUT,
                )
                break

            drained = False
            while True:
                try:
                    buf.append(token_queue.get_nowait())
                    drained = True
                except _queue.Empty:
                    break

            if not drained:
                await asyncio.sleep(0.4)
                continue

            raw = _MARKER.sub("", "".join(buf))
            text = _clean_response(raw)
            now = time.monotonic()

            # Only show up to the last paragraph break while streaming;
            # the trailing incomplete line is held back to avoid flashing
            # progress narration that will be stripped later.
            last_break = text.rfind("\n\n")
            display = text[:last_break].rstrip() if last_break > 0 else ""

            if display and display != live_text and (now - last_edit) >= THROTTLE:
                try:
                    if live_msg is None:
                        live_msg = await update.message.reply_text(
                            display[:4096],
                        )
                        live_text = display[:4096]
                    elif len(display) <= 4096:
                        await live_msg.edit_text(display)
                        live_text = display
                    else:
                        await live_msg.edit_text(display[:4096])
                        live_msg = None
                        live_text = ""
                        buf = [display[4096:] + text[last_break:]]
                    sent_any = True
                except Exception:
                    pass
                last_edit = now

            await asyncio.sleep(0.4)

        # ── Final drain ───────────────────────────────────────────────
        response = future.result() if future.done() else "(timed out)"
        while True:
            try:
                buf.append(token_queue.get_nowait())
            except _queue.Empty:
                break

        raw = _MARKER.sub("", "".join(buf))
        remaining = _clean_response(raw.strip())
        if remaining and remaining != live_text:
            try:
                if live_msg and len(remaining) <= 4096:
                    await live_msg.edit_text(remaining)
                elif live_msg:
                    await live_msg.edit_text(remaining[:4096])
                    for chunk in _split_message(remaining[4096:]):
                        await update.message.reply_text(chunk)
                else:
                    for chunk in _split_message(remaining):
                        await update.message.reply_text(chunk)
                sent_any = True
            except Exception:
                pass

        if not sent_any:
            text = _clean_response(response or "(no response)")
            for chunk in _split_message(text):
                await update.message.reply_text(chunk)

    def _register_file_sender(self, loop: asyncio.AbstractEventLoop, chat_id: int) -> None:
        """Register a sync callback so the Agent can send files via Telegram."""
        from ..core.tools import set_file_sender

        bot_app = self._app

        def _sender(path: str, caption: str = "") -> None:
            async def _do_send():
                try:
                    with open(path, "rb") as f:
                        await bot_app.bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            caption=caption[:1024] if caption else None,
                        )
                except Exception as exc:
                    logger.warning("[Telegram] send_file failed: %s", exc)

            future = asyncio.run_coroutine_threadsafe(_do_send(), loop)
            future.result(timeout=60)

        set_file_sender(_sender)

    async def _build_image_input(self, update: Update, caption: str) -> list:
        """Download photo and build a multimodal content array."""
        photo = update.message.photo[-1]  # highest resolution
        file = await photo.get_file()
        data = await file.download_as_bytearray()
        b64 = base64.b64encode(bytes(data)).decode()

        return [
            {"type": "text", "text": caption},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}",
                },
            },
        ]

    async def _transcribe_voice(self, update: Update) -> str | None:
        """Download a voice/audio message and transcribe via Deepgram.

        Returns the transcript text, or sends a hint to the user and
        returns ``None`` if Deepgram is not configured.
        """
        from ..core.stt import no_key_message, transcribe_bytes_async

        voice = update.message.voice or update.message.audio
        tg_file = await voice.get_file()
        audio_bytes = bytes(await tg_file.download_as_bytearray())
        mime = voice.mime_type or "audio/ogg"

        try:
            transcript = await transcribe_bytes_async(audio_bytes, mime)
        except Exception as exc:
            logger.warning("[Telegram] Deepgram transcription failed: %s", exc)
            await update.message.reply_text(f"Voice transcription failed: {exc}")
            return None

        if transcript is None:
            await update.message.reply_text(no_key_message())
            return None

        if not transcript.strip():
            await update.message.reply_text("Could not recognise any speech in the audio.")
            return None

        logger.info("[Telegram] Voice transcribed: %s", transcript[:80])
        return transcript

    async def _keep_typing(self, chat_id: int) -> None:
        """Re-send the 'typing' chat action every 4 s until cancelled."""
        try:
            while True:
                await self._app.bot.send_chat_action(chat_id=chat_id, action="typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("[Telegram] _keep_typing stopped unexpectedly", exc_info=True)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    _BOT_COMMANDS = [
        BotCommand("start", "显示欢迎信息"),
        BotCommand("reset", "重置当前会话"),
        BotCommand("status", "查看会话状态"),
        BotCommand("compact", "压缩对话历史"),
        BotCommand("my_profile", "查看我的个性化资料"),
        BotCommand("update_profile", "更新我的明确偏好"),
        BotCommand("feedback", "记录提醒反馈"),
        BotCommand("privacy", "查看隐私说明"),
        BotCommand("delete_profile", "删除我的个性化数据"),
        BotCommand("clear_inferred", "清空 Jarvis 的推断偏好（保留明确设置）"),
        BotCommand("bank_rates", "查看十大银行 AUD 牌价"),
        BotCommand("fx_research", "生成 CNY/AUD 研究简报（多代理分析）"),
        BotCommand("clear_files", "清空下载文件"),
    ]

    def build_application(self) -> Application:
        app = Application.builder().token(self._token).build()
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("reset", self._cmd_reset))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("compact", self._cmd_compact))
        app.add_handler(CommandHandler("my_profile", self._cmd_my_profile))
        app.add_handler(CommandHandler("update_profile", self._cmd_update_profile))
        app.add_handler(CommandHandler("feedback", self._cmd_feedback))
        app.add_handler(CommandHandler("privacy", self._cmd_privacy))
        app.add_handler(CommandHandler("delete_profile", self._cmd_delete_profile))
        app.add_handler(CommandHandler("clear_inferred", self._cmd_clear_inferred))
        app.add_handler(CommandHandler("bank_rates", self._cmd_bank_rates))
        app.add_handler(CommandHandler("fx_research", self._cmd_fx_research))
        app.add_handler(CommandHandler("clear_files", self._cmd_clear_files))
        app.add_handler(CallbackQueryHandler(
            self._handle_feedback_callback, pattern=r"^fb:"
        ))
        app.add_handler(MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO)
            & ~filters.COMMAND,
            self._handle_message,
        ))
        self._app = app
        return app

    async def _register_commands(self) -> None:
        """Register slash-commands with Telegram so they appear in the menu."""
        try:
            await self._app.bot.set_my_commands(self._BOT_COMMANDS)
            me = await self._app.bot.get_me()
            self._bot_username = me.username
            logger.info(
                "[Telegram] Registered %d bot commands, username=@%s",
                len(self._BOT_COMMANDS), self._bot_username,
            )
        except Exception:
            logger.warning("[Telegram] Failed to register bot commands", exc_info=True)

    def run_polling(self) -> None:
        """Blocking call — starts the bot using long polling (for standalone use)."""
        app = self.build_application()
        logger.info("[Telegram] Starting bot (polling mode)...")
        app.post_init = lambda _app: self._register_commands()
        app.run_polling(drop_pending_updates=True)

    async def start_async(self) -> None:
        """Non-blocking start — for use inside an existing asyncio event loop."""
        app = self.build_application()
        logger.info("[Telegram] Initialising bot (async mode)...")
        await app.initialize()
        await app.start()
        await self._register_commands()
        await app.updater.start_polling(drop_pending_updates=True)

    async def stop_async(self) -> None:
        if self._app is None:
            return
        logger.info("[Telegram] Stopping bot...")
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()


def create_bot(session_manager: "SessionManager") -> TelegramBot:
    """Create a TelegramBot from pythonclaw.json / env vars."""
    token = config.get_str(
        "channels", "telegram", "token", env="TELEGRAM_BOT_TOKEN",
    )
    if not token:
        raise ValueError("Telegram token not set (env TELEGRAM_BOT_TOKEN or channels.telegram.token)")
    allowed_users = config.get_int_list(
        "channels", "telegram", "allowedUsers", env="TELEGRAM_ALLOWED_USERS",
    )
    require_mention = config.get_bool(
        "channels", "telegram", "requireMention", default=False,
    )
    return TelegramBot(
        session_manager=session_manager,
        token=token,
        allowed_users=allowed_users or None,
        require_mention=require_mention,
    )


# Backward-compatible alias
create_bot_from_env = create_bot
