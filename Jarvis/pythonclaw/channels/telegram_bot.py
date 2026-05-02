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

from telegram import BotCommand, ReactionTypeEmoji, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .. import config

if TYPE_CHECKING:
    from ..session_manager import SessionManager

logger = logging.getLogger(__name__)

# ── Skill directory & file paths ──────────────────────────────────────────────

_SKILL_DIR = (
    Path(__file__).parent.parent / "templates" / "skills" / "data" / "cnyaud_monitor"
)
_GREETED_FILE = config.PYTHONCLAW_HOME / "context" / "greeted_users.json"
_NEWS_CACHE_FILE = config.PYTHONCLAW_HOME / "context" / "news_recent_cache.json"


# ── Welcome messages ──────────────────────────────────────────────────────────

_WELCOME_GUIDE = (
    "👋 欢迎使用 Jarvis AI 助手！\n\n"
    "🤖 我是你的个人智能代理，主要功能：\n"
    "  • 实时监控 CNY/AUD 汇率波动与告警\n"
    "  • 追踪中东局势、澳元走势等关键新闻\n"
    "  • 支持自然语言对话和复杂任务处理\n\n"
    "📋 快捷指令（直接发送以下文字）：\n"
    "  最新新闻   — 拉取最新关键词新闻\n"
    "  最新汇率   — 查看当前 CNY/AUD 汇率\n"
    "  银行牌价   — 查看十大银行 AUD 现汇买入/卖出价\n"
    "  汇率波动   — 查看近2日汇率走势图\n\n"
    "⚙️ 斜杠命令：\n"
    "  /start — 显示此说明\n"
    "  /reset — 重置当前对话\n"
    "  /status — 查看会话状态\n"
    "  /compact — 压缩对话历史\n"
    "  /my_profile — 查看个性化资料；中文：/我的资料\n"
    "  /update_profile — 修改偏好；中文：/修改资料\n"
    "  /feedback useful — 记录反馈；中文：/反馈 有用\n"
    "  /delete_profile — 删除个性化数据；中文：/删除资料\n"
    "  /privacy — 查看隐私说明；中文：/隐私\n\n"
    "📝 常用中文示例：\n"
    "  /我的资料\n"
    "  /修改资料\n"
    "  /反馈 有用\n"
    "  /反馈 不感兴趣 主题=通用市场新闻\n"
    "  /删除资料\n"
    "  /隐私\n\n"
    "💡 也可以直接输入任何问题，我会尽力回答！"
)

_RETURNING_WELCOME = (
    _WELCOME_GUIDE
)

_PRIVACY_TEXT = (
    "Jarvis 第8阶段隐私说明\n\n"
    "Jarvis 会把个性化相关数据分成几类处理：\n\n"
    "1. 明确偏好\n"
    "例如语言、目标汇率、提醒阈值、摘要风格。这些是你主动设置或确认的内容，可用于提供个性化体验。\n\n"
    "2. 推断出的内容偏好\n"
    "例如你可能更关注哪些汇率或新闻主题。这类内容应与明确偏好分开保存，并支持你查看、修改或删除。\n\n"
    "3. 短期原始行为记录\n"
    "例如用于评估功能是否有用的临时事件记录。这类数据只用于短期评估和聚合，"
    "不会作为长期记忆，也不会作为个性化上下文直接提供给大语言模型。\n\n"
    "4. 敏感信息\n"
    "Jarvis 不会主动要求你提供银行卡、账户余额、身份证/护照、确切地址或详细个人财务压力等敏感信息。"
    "检测到这类内容时，Jarvis 会尽量拒绝写入个性化资料。\n\n"
    "大语言模型的个性化上下文只应接收经过白名单筛选的字段，而不是完整用户档案、原始日志或 MEMORY.md。\n\n"
    "资料相关命令：\n"
    "- /my_profile 查看当前个性化资料\n"
    "- /update_profile 修改明确偏好\n"
    "- /feedback 记录提醒反馈\n"
    "- /delete_profile 删除个性化数据"
)

_UPDATE_PROFILE_USAGE = (
    "格式示例：\n"
    "/update_profile 目标汇率=4.85 提醒阈值=0.3 用途=学费 风格=简短 主题=RBA,oil,CNY 偏好银行=中国银行,建设银行,工商银行 提醒偏好=重大新闻\n\n"
    "等号两边可以有空格；提醒阈值=0.3 表示 0.3%。\n"
    "可用字段：目标汇率、提醒阈值、用途、语言、风格、主题、偏好银行、提醒偏好、隐私级别。\n"
    "用途建议：学费、生活、投资、一般。风格可选：简短、普通、详细。"
)

_FEEDBACK_USAGE = (
    "反馈格式示例：\n"
    "/feedback useful\n"
    "/feedback not_useful\n"
    "/feedback not_interested topic=通用市场新闻\n\n"
    "中文也可以：/反馈 有用、/反馈 无用、/反馈 不感兴趣 主题=通用市场新闻"
)


# ── Module-level helpers ──────────────────────────────────────────────────────

def _is_new_user(chat_id: int) -> bool:
    """Return True if this chat_id has never been shown the welcome guide."""
    if not _GREETED_FILE.exists():
        return True
    try:
        data = json.loads(_GREETED_FILE.read_text(encoding="utf-8"))
        return str(chat_id) not in data.get("greeted", [])
    except Exception:
        return True


def _mark_greeted(chat_id: int) -> None:
    """Record that this chat_id has received the welcome guide."""
    data: dict = {"greeted": []}
    if _GREETED_FILE.exists():
        try:
            data = json.loads(_GREETED_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    greeted = set(data.get("greeted", []))
    greeted.add(str(chat_id))
    data["greeted"] = sorted(greeted)
    _GREETED_FILE.parent.mkdir(parents=True, exist_ok=True)
    _GREETED_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _get_recent_news_text(n: int = 5) -> str:
    """Return formatted text of the n most recently cached news articles."""
    if not _NEWS_CACHE_FILE.exists():
        return ""
    try:
        data = json.loads(_NEWS_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ""
    articles = data.get("articles", [])[:n]
    if not articles:
        return ""
    lines = [
        "═══════════════════════════════════",
        "  📰 最近新闻快报（缓存）",
        "═══════════════════════════════════",
        f"更新时间: {data.get('updated_at', 'N/A')}",
        f"显示最近 {len(articles)} 条",
        "",
    ]
    for art in articles:
        lines.append(f"[{art.get('keyword', '综合')}]")
        lines.append(f"  标题: {art['title']}")
        lines.append(f"  时间: {art.get('published', 'N/A')}")
        if art.get("snippet"):
            lines.append(f"  摘要: {art['snippet']}")
        lines.append(f"  链接: {art['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _load_cnyaud(module_name: str):
    """Dynamically load a cnyaud_monitor skill module by filename (no .py)."""
    spec = importlib.util.spec_from_file_location(
        f"_cnyaud_{module_name}",
        _SKILL_DIR / f"{module_name}.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _preferred_bank_names_for_user(telegram_user_id: int | None) -> list[str]:
    from ..core.personalization import get_user_profile
    from ..core.personalization.user_profile_store import DEFAULT_PREFERRED_BANKS

    if telegram_user_id is None:
        return list(DEFAULT_PREFERRED_BANKS)
    try:
        profile = get_user_profile(telegram_user_id)
    except Exception:
        logger.exception("[Telegram] Failed to load preferred banks for user_id=%s", telegram_user_id)
        return list(DEFAULT_PREFERRED_BANKS)
    explicit = profile.get("explicit_preferences") or {}
    banks = explicit.get("preferred_banks") or []
    return banks or list(DEFAULT_PREFERRED_BANKS)


def _format_bank_rate_table(
    data: dict[str, Any],
    expected_sources: list[tuple[str, str, str]] | None = None,
    *,
    preferred_banks: list[str] | None = None,
    show_all: bool = False,
) -> str:
    """Render AUD bank quotes for Telegram without using an LLM."""
    quotes = list((data.get("bank_exchange_rates") or {}).get("quotes") or [])
    if not quotes:
        error = data.get("error") or "未获取到银行牌价。"
        return f"⚠️ 银行牌价获取失败：{error}"

    if preferred_banks and not show_all:
        wanted = set(preferred_banks)
        quotes = [q for q in quotes if q.get("bank") in wanted]
        if not quotes:
            return f"⚠️ 未获取到关注银行牌价：{'、'.join(preferred_banks)}"

    quotes.sort(key=lambda q: (q.get("spot_sell_rate") is None, q.get("spot_sell_rate") or 999))
    expected_names = (
        [name for _, name, _ in (expected_sources or [])]
        if show_all or not preferred_banks
        else preferred_banks
    )
    fetched_names = {q.get("bank") for q in quotes}
    missing = [name for name in expected_names if name not in fetched_names]

    summary = (data.get("bank_exchange_rates") or {}).get("summary") or {}
    ref = data.get("student_exchange_reference") or summary.get("best_for_buying_aud_with_cny") or {}
    if quotes:
        ref_quote = min(quotes, key=lambda q: q.get("spot_sell_rate") or 999)
        ref = {
            "bank": ref_quote.get("bank"),
            "rate_1_aud_in_cny": ref_quote.get("spot_sell_rate"),
        }
        high_quote = max(quotes, key=lambda q: q.get("spot_sell_rate") or -1)
    else:
        high_quote = None

    lines = [
        "🏦 AUD 十大银行牌价（绕过 LLM）" if show_all else "🏦 AUD 关注银行牌价（绕过 LLM）",
        f"时间: {data.get('fetched_at_utc', 'N/A')} UTC",
        "",
        "口径：1 AUD = X CNY",
        "买 AUD 看现汇卖出价；卖 AUD 看现汇买入价。",
    ]
    if ref:
        lines.append(
            f"当前买 AUD 最低参考: {ref.get('bank', 'N/A')} {ref.get('rate_1_aud_in_cny', 'N/A')}"
        )
    if high_quote and high_quote.get("spot_sell_rate"):
        lines.append(
            f"当前买 AUD 最高参考: {high_quote.get('bank', 'N/A')} {high_quote.get('spot_sell_rate')}"
        )
    if data.get("market_1_AUD_in_CNY"):
        lines.append(f"市场中间价参考: {data['market_1_AUD_in_CNY']:.4f}（非实际银行成交价）")
    lines.extend([
        "",
        "银行       买入价   卖出价   更新时间",
        "----------------------------------------",
    ])
    for q in quotes:
        bank = str(q.get("bank", "N/A"))[:5]
        buy = q.get("spot_buy_rate")
        sell = q.get("spot_sell_rate")
        published = str(q.get("published_at") or "").replace("2026-", "")
        lines.append(
            f"{bank:<5}  "
            f"{buy:.4f}" if isinstance(buy, (int, float)) else f"{bank:<5}  {'N/A':>6}"
        )
        lines[-1] += (
            f"  {sell:.4f}" if isinstance(sell, (int, float)) else f"  {'N/A':>6}"
        )
        if published:
            lines[-1] += f"  {published}"

    if summary.get("median_spot_sell_rate"):
        sell_values = [q.get("spot_sell_rate") for q in quotes if isinstance(q.get("spot_sell_rate"), (int, float))]
        lines.extend([
            "",
            f"卖出价中位数: {statistics.median(sell_values):.4f}" if sell_values else f"卖出价中位数: {summary['median_spot_sell_rate']:.4f}",
            f"卖出价区间: {min(sell_values):.4f} - {max(sell_values):.4f}" if sell_values else f"卖出价区间: {summary.get('lowest_spot_sell_rate', 'N/A')} - {summary.get('highest_spot_sell_rate', 'N/A')}",
        ])
    if summary.get("median_bank_spread_pct") is not None:
        lines.append(f"买卖价差中位: {summary['median_bank_spread_pct']:.3f}%")
    if missing:
        lines.append(f"未获取: {'、'.join(missing)}")
    lines.append("实际成交价以银行 App/柜台为准。")
    return "\n".join(lines)


def _display_value(value: Any) -> str:
    if value is None or value == "":
        return "未设置"
    return str(value)


def _display_topics(value: Any) -> str:
    if not value:
        return "未设置"
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return "、".join(items) if items else "未设置"
    return str(value)


def _display_percent(value: Any) -> str:
    if value is None or value == "":
        return "未设置"
    return f"{value}%"


_PROFILE_VALUE_LABELS = {
    "purpose": {
        "tuition": "学费",
        "living": "生活",
        "investment": "投资",
        "general": "其他",
    },
    "preferred_summary_style": {
        "brief": "简短",
        "standard": "普通",
        "detailed": "详细",
        "action_first": "行动优先",
    },
    "privacy_level": {
        "minimal": "最少",
        "standard": "标准",
        "strict": "严格",
    },
    "alert_preference": {
        "target_rate": "目标汇率",
        "volatility": "波动率",
        "major_news": "重大新闻",
        "morning_report": "晨报",
    },
}


def _display_profile_value(field: str, value: Any) -> str:
    if value is None or value == "":
        return "未设置"
    return _PROFILE_VALUE_LABELS.get(field, {}).get(str(value), str(value))


def _is_set(value: Any) -> bool:
    return value not in (None, "", [])


def _format_feedback_summary(summary: dict[str, Any]) -> list[str]:
    if not summary:
        return []
    labels = {
        "useful": "有用",
        "not_useful": "无用",
        "useless": "无用",
        "not_interested": "不感兴趣",
    }
    lines = ["", "反馈记录："]
    for key in ("useful", "not_useful", "useless", "not_interested"):
        count = summary.get(key)
        if count:
            lines.append(f"- {labels[key]}：{count}")
    return lines if len(lines) > 2 else []


def _format_user_profile(profile: dict[str, Any], *, created: bool = False) -> str:
    """Render structured personalization data without raw behavior logs."""
    explicit = profile.get("explicit_preferences") or {}
    inferred = profile.get("inferred_preferences") or {}
    feedback = profile.get("feedback_summary") or {}

    lines = ["你的 Jarvis 个性化资料"]
    if created:
        lines.extend([
            "",
            "我刚为你创建了默认资料。目前暂无偏好记录。",
            "",
            "说明：这里不会显示原始行为日志或短期 raw events。",
            "你可以使用 /update_profile 修改明确偏好，或使用 /delete_profile 删除个性化数据。",
        ])
        return "\n".join(lines)

    explicit_lines = [
        ("目标汇率", _display_value(explicit.get("target_rate")), explicit.get("target_rate")),
        ("提醒阈值", _display_percent(explicit.get("alert_threshold")), explicit.get("alert_threshold")),
        ("用途", _display_profile_value("purpose", explicit.get("purpose")), explicit.get("purpose")),
        ("提醒偏好", _display_profile_value("alert_preference", explicit.get("alert_preference")), explicit.get("alert_preference")),
        ("语言", _display_value(explicit.get("language")), explicit.get("language")),
        (
            "首选摘要样式",
            _display_profile_value("preferred_summary_style", explicit.get("preferred_summary_style")),
            explicit.get("preferred_summary_style"),
        ),
        ("首选主题", _display_topics(explicit.get("preferred_topics")), explicit.get("preferred_topics")),
        ("偏好银行", _display_topics(explicit.get("preferred_banks")), explicit.get("preferred_banks")),
        ("首选提醒时间", _display_value(explicit.get("preferred_reminder_time")), explicit.get("preferred_reminder_time")),
        ("可操作性阈值", _display_value(explicit.get("actionability_threshold")), explicit.get("actionability_threshold")),
        ("隐私级别", _display_profile_value("privacy_level", explicit.get("privacy_level")), explicit.get("privacy_level")),
    ]
    lines.extend(["", "明确偏好："])
    added = False
    for label, display, raw in explicit_lines:
        if _is_set(raw):
            lines.append(f"- {label}：{display}")
            added = True
    if not added:
        lines.append("- 暂无明确偏好")

    lines.extend(["", "推断出的偏好："])
    inferred_added = False
    if _is_set(inferred.get("high_interest_topics")):
        lines.append(f"- 高兴趣话题：{_display_topics(inferred.get('high_interest_topics'))}")
        inferred_added = True
    if _is_set(inferred.get("low_interest_topics")):
        lines.append(f"- 低兴趣话题：{_display_topics(inferred.get('low_interest_topics'))}")
        inferred_added = True
    confidence = inferred.get("confidence")
    if confidence is not None:
        lines.append(f"- 推断置信度：{confidence}")
        inferred_added = True
    if not inferred_added:
        lines.append("- 暂无推断偏好")

    lines.extend(_format_feedback_summary(feedback))
    lines.extend([
        "",
        "说明：这里不会显示原始行为日志或短期 raw events。",
        "你可以使用 /update_profile 修改明确偏好，或使用 /delete_profile 删除个性化数据。",
    ])
    return "\n".join(lines)


def _delete_profile_requires_confirmation() -> bool:
    """Hook for adding a multi-step delete confirmation flow later."""
    return True


_PROFILE_FIELD_ALIASES = {
    "target_rate": "target_rate",
    "目标汇率": "target_rate",
    "汇率目标": "target_rate",
    "alert_threshold": "alert_threshold",
    "提醒阈值": "alert_threshold",
    "告警阈值": "alert_threshold",
    "purpose": "purpose",
    "用途": "purpose",
    "language": "language",
    "语言": "language",
    "style": "preferred_summary_style",
    "preferred_summary_style": "preferred_summary_style",
    "风格": "preferred_summary_style",
    "摘要风格": "preferred_summary_style",
    "topics": "preferred_topics",
    "preferred_topics": "preferred_topics",
    "主题": "preferred_topics",
    "关注主题": "preferred_topics",
    "banks": "preferred_banks",
    "bank": "preferred_banks",
    "preferred_banks": "preferred_banks",
    "银行": "preferred_banks",
    "偏好银行": "preferred_banks",
    "关注银行": "preferred_banks",
    "privacy_level": "privacy_level",
    "隐私级别": "privacy_level",
    "隐私": "privacy_level",
    "alert_preference": "alert_preference",
    "提醒偏好": "alert_preference",
    "提醒": "alert_preference",
}

_PURPOSE_ALIASES = {
    "tuition": "tuition",
    "学费": "tuition",
    "living": "living",
    "生活": "living",
    "生活费": "living",
    "investment": "investment",
    "投资": "investment",
    "general": "general",
    "一般": "general",
    "通用": "general",
    "其他": "general",
}

_STYLE_ALIASES = {
    "short": "brief",
    "简短": "brief",
    "短": "brief",
    "brief": "brief",
    "normal": "standard",
    "普通": "standard",
    "正常": "standard",
    "standard": "standard",
    "detailed": "detailed",
    "详细": "detailed",
}

_PRIVACY_ALIASES = {
    "minimal": "minimal",
    "最少": "minimal",
    "最小": "minimal",
    "standard": "standard",
    "标准": "standard",
    "strict": "strict",
    "严格": "strict",
}

_ALERT_PREFERENCE_ALIASES = {
    "target_rate": "target_rate",
    "目标汇率": "target_rate",
    "volatility": "volatility",
    "波动率": "volatility",
    "汇率波动": "volatility",
    "major_news": "major_news",
    "重大新闻": "major_news",
    "新闻": "major_news",
    "morning_report": "morning_report",
    "晨报": "morning_report",
    "早报": "morning_report",
}

_UPDATE_PROFILE_LABELS = {
    "target_rate": "目标汇率",
    "alert_threshold": "提醒阈值",
    "purpose": "用途",
    "language": "语言",
    "preferred_summary_style": "摘要风格",
    "preferred_topics": "关注主题",
    "preferred_banks": "偏好银行",
    "privacy_level": "隐私级别",
    "alert_preference": "提醒偏好",
}

_UPDATE_PROFILE_VALUE_LABELS = {
    "purpose": {
        "tuition": "学费",
        "living": "生活",
        "investment": "投资",
        "general": "一般",
    },
    "preferred_summary_style": {
        "brief": "简短",
        "standard": "普通",
        "detailed": "详细",
    },
    "privacy_level": {
        "minimal": "最少",
        "standard": "标准",
        "strict": "严格",
    },
    "alert_preference": {
        "target_rate": "目标汇率",
        "volatility": "波动率",
        "major_news": "重大新闻",
        "morning_report": "晨报",
    },
}

_UPDATE_PROFILE_PAIR_RE = re.compile(
    r"([^\s=]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s]+))"
)
_FEEDBACK_TOPIC_RE = re.compile(r"(?:^|\s)(?:topic|主题)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|(.+))$")

_UPDATE_PROFILE_ERROR_MESSAGES = {
    "empty": "请在 /update_profile 后添加要更新的字段。",
    "bad_format": "格式不正确，请使用 key=value，例如：目标汇率=4.85。",
    "target_rate": "目标汇率必须是合理数字，例如 4.85。",
    "alert_threshold": "提醒阈值必须是 0 到 10 之间的百分比数字，例如 0.3 表示 0.3%。",
    "purpose": "用途建议使用：学费、生活、投资、一般。",
    "style": "风格可选：简短、普通、详细。",
    "topics": "主题不能为空，可用逗号、中文逗号或顿号分隔，例如：RBA，oil，CNY。",
    "preferred_banks": "偏好银行可选：中国银行、工商银行、建设银行、农业银行、交通银行、招商银行、中信银行、兴业银行、光大银行、浦发银行。",
    "privacy_level": "隐私级别可选：最少、标准、严格。",
    "alert_preference": "提醒偏好可选：目标汇率、波动率、重大新闻、晨报。",
}

_UPDATE_PROFILE_WIZARD_KEY = "update_profile_wizard"
_DELETE_PROFILE_PENDING_KEY = "delete_profile_pending"
_UPDATE_PROFILE_WIZARD_STEPS = [
    ("target_rate", "请输入目标汇率，例如：4.85"),
    ("alert_threshold", "请输入提醒阈值百分比，例如：0.3 表示 0.3%"),
    ("purpose", "请输入用途：学费、生活、投资、一般"),
    ("preferred_summary_style", "请输入摘要风格：简短、普通、详细"),
    ("preferred_topics", "请输入关注主题，可用逗号或顿号分隔，例如：RBA，oil，CNY"),
    ("preferred_banks", "请输入偏好银行，可用逗号或顿号分隔，例如：中国银行，建设银行，工商银行"),
    ("language", "请输入语言，例如：zh-CN 或 中文"),
    ("privacy_level", "请输入隐私级别：最少、标准、严格"),
]

_ONBOARDING_KEY = "profile_onboarding"
_ONBOARDING_STEPS = [
    ("purpose", "你的主要用途是什么？\n请选择：学费 / 生活费 / 投资 / 其他"),
    ("alert_preference", "你更希望 Jarvis 优先提醒什么？\n请选择：目标汇率 / 波动率 / 重大新闻 / 晨报"),
    ("preferred_summary_style", "你喜欢哪种摘要风格？\n请选择：简短 / 普通 / 详细"),
]
_ONBOARDING_INTRO = (
    "为了让 Jarvis 的提醒更贴近你的使用方式，我会先问 3 个简单问题。\n"
    "不会询问余额、银行信息、证件号或地址。\n\n"
    "你也可以回复“跳过引导”，直接使用默认资料；跳过后不会反复询问。"
)

_FEEDBACK_TYPE_ALIASES = {
    "useful": "useful",
    "有用": "useful",
    "not_useful": "not_useful",
    "无用": "not_useful",
    "useless": "not_useful",
    "not_interested": "not_interested",
    "不感兴趣": "not_interested",
}
_FEEDBACK_TYPE_LABELS = {
    "useful": "有用",
    "not_useful": "无用",
    "not_interested": "不感兴趣",
}


def _parse_update_profile_value(field: str, value: str) -> Any:
    value = value.strip()
    if not value:
        raise ValueError("bad_format")

    if field == "target_rate":
        parsed = float(value)
        if not math.isfinite(parsed) or parsed < 3.0 or parsed > 8.0:
            raise ValueError("target_rate")
        return parsed
    if field == "alert_threshold":
        parsed = float(value.rstrip("%"))
        if not math.isfinite(parsed) or parsed <= 0 or parsed > 10:
            raise ValueError("alert_threshold")
        return parsed
    if field == "purpose":
        parsed = _PURPOSE_ALIASES.get(value.lower(), _PURPOSE_ALIASES.get(value))
        if parsed is None:
            raise ValueError("purpose")
        return parsed
    if field == "preferred_summary_style":
        parsed = _STYLE_ALIASES.get(value.lower(), _STYLE_ALIASES.get(value))
        if parsed is None:
            raise ValueError("style")
        return parsed
    if field == "alert_preference":
        parsed = _ALERT_PREFERENCE_ALIASES.get(
            value.lower(),
            _ALERT_PREFERENCE_ALIASES.get(value),
        )
        if parsed is None:
            raise ValueError("alert_preference")
        return parsed
    if field == "preferred_topics":
        normalized = value.replace("，", ",").replace("、", ",")
        parsed = [item.strip() for item in normalized.split(",") if item.strip()]
        if not parsed:
            raise ValueError("topics")
        return parsed
    if field == "preferred_banks":
        from ..core.personalization.user_profile_store import normalize_preferred_banks
        try:
            parsed = normalize_preferred_banks(value)
        except ValueError:
            raise ValueError("preferred_banks") from None
        if not parsed:
            raise ValueError("preferred_banks")
        return parsed
    if field == "privacy_level":
        parsed = _PRIVACY_ALIASES.get(value.lower(), _PRIVACY_ALIASES.get(value))
        if parsed is None:
            raise ValueError("privacy_level")
        return parsed
    if field == "language":
        return value
    raise ValueError("bad_format")


def _parse_update_profile_args(args: list[str]) -> dict[str, Any]:
    raw = " ".join(args).strip()
    if not raw:
        raise ValueError("empty")

    updates: dict[str, Any] = {}
    position = 0
    for match in _UPDATE_PROFILE_PAIR_RE.finditer(raw):
        if raw[position:match.start()].strip():
            raise ValueError("bad_format")
        position = match.end()
        key = match.group(1).strip()
        value = next(group for group in match.groups()[1:] if group is not None).strip()
        field = _PROFILE_FIELD_ALIASES.get(key) or _PROFILE_FIELD_ALIASES.get(key.lower())
        if not field:
            raise ValueError("bad_format")
        updates[field] = _parse_update_profile_value(field, value)

    if raw[position:].strip() or not updates:
        raise ValueError("bad_format")
    return updates


def _format_update_profile_error(code: str) -> str:
    message = _UPDATE_PROFILE_ERROR_MESSAGES.get(code, _UPDATE_PROFILE_ERROR_MESSAGES["bad_format"])
    return f"{message}\n\n{_UPDATE_PROFILE_USAGE}"


def _update_profile_prompt(step_index: int) -> str:
    _, prompt = _UPDATE_PROFILE_WIZARD_STEPS[step_index]
    return (
        f"{prompt}\n\n"
        "输入“跳过”可跳过这一项，输入“取消”可退出修改。"
    )


def _onboarding_prompt(step_index: int) -> str:
    _, prompt = _ONBOARDING_STEPS[step_index]
    return (
        f"{prompt}\n\n"
        "回复“跳过”可跳过这一题，回复“跳过引导”可结束引导。"
    )


def _format_update_profile_confirmation(updates: dict[str, Any]) -> str:
    lines = ["已更新你的明确偏好："]
    for key, value in updates.items():
        label = _UPDATE_PROFILE_LABELS.get(key, key)
        if isinstance(value, list):
            display = "、".join(str(item) for item in value)
        else:
            display = _UPDATE_PROFILE_VALUE_LABELS.get(key, {}).get(str(value), str(value))
        lines.append(f"- {label}：{display}")
    lines.append("")
    lines.append("这些字段会进入结构化个性化资料；不会更新推断偏好或 raw events。")
    return "\n".join(lines)


def _parse_feedback_args(args: list[str]) -> tuple[str, str | None]:
    raw = " ".join(args).strip()
    if not raw:
        raise ValueError("empty")

    topic: str | None = None
    topic_match = _FEEDBACK_TOPIC_RE.search(raw)
    if topic_match:
        topic = next(group for group in topic_match.groups() if group is not None).strip()
        raw = raw[:topic_match.start()].strip()

    feedback_type = _FEEDBACK_TYPE_ALIASES.get(raw) or _FEEDBACK_TYPE_ALIASES.get(raw.lower())
    if feedback_type is None:
        raise ValueError("type")
    return feedback_type, topic


def _format_feedback_confirmation(event_type: str, topic: str | None) -> str:
    label = _FEEDBACK_TYPE_LABELS.get(event_type, event_type)
    if topic:
        return f"已记录反馈：{label}（主题：{topic}）。谢谢。"
    return f"已记录反馈：{label}。谢谢。"


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
        except ValueError:
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

    async def _cmd_feedback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update, context):
            return
        await self._record_feedback(update, list(context.args or []))

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
        return False

    async def _cmd_delete_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update, context):
            return
        await self._handle_delete_profile_command(update, context, list(context.args or []))

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
        BotCommand("bank_rates", "查看十大银行 AUD 牌价"),
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
        app.add_handler(CommandHandler("bank_rates", self._cmd_bank_rates))
        app.add_handler(CommandHandler("clear_files", self._cmd_clear_files))
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


# ── Utility ───────────────────────────────────────────────────────────────────

_LEAKED_TOOL_RE = re.compile(
    r'<\s*\|?\s*(?:DSML|antml)\s*\|\s*function_calls[^>]*>'
    r'[\s\S]*?'
    r'<\s*/\s*\|?\s*(?:DSML|antml)\s*\|\s*function_calls\s*>',
    re.IGNORECASE,
)


_PROGRESS_LINE_RE = re.compile(r'\n\n.{0,60}[：:]\s*\n\n')


def _clean_response(text: str) -> str:
    """Strip leaked tool-call XML/DSML markup and excess whitespace."""
    text = _LEAKED_TOOL_RE.sub('', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    for _ in range(10):
        cleaned = _PROGRESS_LINE_RE.sub('\n\n', text)
        if cleaned == text:
            break
        text = cleaned
    return text.strip()


def _split_message(text: str, limit: int = 4096) -> list[str]:
    """Split text into chunks respecting natural boundaries.

    Tries paragraph breaks first, then newlines, then word boundaries,
    and only falls back to a hard character cut as a last resort.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    min_break = limit // 3
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind('\n\n', min_break, limit)
        if split_at < min_break:
            split_at = text.rfind('\n', min_break, limit)
        if split_at < min_break:
            split_at = text.rfind(' ', min_break, limit)
        if split_at < min_break:
            split_at = limit
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    return chunks


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
