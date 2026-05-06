"""
Formatting helpers, constants, and pure utility functions for telegram_bot.py.

Extracted to keep TelegramBot class focused on I/O and handler wiring.
No Telegram handler logic lives here — only data-shaping code.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import math
import re
import statistics
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .. import config

logger = logging.getLogger(__name__)

# ── Skill directory & file paths ──────────────────────────────────────────────

_SKILL_DIR = (
    Path(__file__).parent.parent / "templates" / "skills" / "data" / "fx_monitor"
)
_RESEARCH_DIR = _SKILL_DIR / "research"
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
    "  /clear_inferred — 清空推断偏好；中文：/清空推断\n"
    "  /delete_profile — 删除个性化数据；中文：/删除资料\n"
    "  /privacy — 查看隐私说明；中文：/隐私\n"
    "  /fx_research — 生成 CNY/AUD 研究简报（多代理 AI 分析）\n\n"
    "📝 常用中文示例：\n"
    "  /我的资料\n"
    "  /修改资料\n"
    "  /反馈 有用\n"
    "  /反馈 不感兴趣 主题=通用市场新闻\n"
    "  /删除资料\n"
    "  /隐私\n\n"
    "💡 也可以直接输入任何问题，我会尽力回答！"
)

_RETURNING_WELCOME = _WELCOME_GUIDE

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


# ── Greeting helpers ──────────────────────────────────────────────────────────

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


def _load_cnyaud(module_name: str) -> Any:
    """Dynamically load an fx_monitor skill module by filename (no .py)."""
    spec = importlib.util.spec_from_file_location(
        f"_cnyaud_{module_name}",
        _SKILL_DIR / f"{module_name}.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── Bank rate formatting ───────────────────────────────────────────────────────

def _preferred_bank_names_for_user(telegram_user_id: int | None) -> list[str]:
    from ..core.personalization import get_user_profile
    from ..core.personalization.user_profile_store import DEFAULT_PREFERRED_BANKS

    if telegram_user_id is None:
        return list(DEFAULT_PREFERRED_BANKS)
    try:
        profile = get_user_profile(telegram_user_id)
    except Exception:
        logger.exception(
            "[Telegram] Failed to load preferred banks for user_id=%s", telegram_user_id
        )
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
        sell_values = [
            q.get("spot_sell_rate") for q in quotes
            if isinstance(q.get("spot_sell_rate"), (int, float))
        ]
        lines.extend([
            "",
            f"卖出价中位数: {statistics.median(sell_values):.4f}"
            if sell_values else
            f"卖出价中位数: {summary['median_spot_sell_rate']:.4f}",
            f"卖出价区间: {min(sell_values):.4f} - {max(sell_values):.4f}"
            if sell_values else
            f"卖出价区间: {summary.get('lowest_spot_sell_rate', 'N/A')} - {summary.get('highest_spot_sell_rate', 'N/A')}",
        ])
    if summary.get("median_bank_spread_pct") is not None:
        lines.append(f"买卖价差中位: {summary['median_bank_spread_pct']:.3f}%")
    if missing:
        lines.append(f"未获取: {'、'.join(missing)}")
    lines.append("实际成交价以银行 App/柜台为准。")
    return "\n".join(lines)


# ── Profile display helpers ───────────────────────────────────────────────────

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


_PROFILE_VALUE_LABELS: dict[str, dict[str, str]] = {
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
        ("目标汇率",   _display_value(explicit.get("target_rate")),           explicit.get("target_rate")),
        ("提醒阈值",   _display_percent(explicit.get("alert_threshold")),      explicit.get("alert_threshold")),
        ("用途",       _display_profile_value("purpose", explicit.get("purpose")),          explicit.get("purpose")),
        ("提醒偏好",   _display_profile_value("alert_preference", explicit.get("alert_preference")), explicit.get("alert_preference")),
        ("语言",       _display_value(explicit.get("language")),               explicit.get("language")),
        ("首选摘要样式", _display_profile_value("preferred_summary_style", explicit.get("preferred_summary_style")), explicit.get("preferred_summary_style")),
        ("首选主题",   _display_topics(explicit.get("preferred_topics")),      explicit.get("preferred_topics")),
        ("偏好银行",   _display_topics(explicit.get("preferred_banks")),       explicit.get("preferred_banks")),
        ("首选提醒时间", _display_value(explicit.get("preferred_reminder_time")), explicit.get("preferred_reminder_time")),
        ("可操作性阈值", _display_value(explicit.get("actionability_threshold")), explicit.get("actionability_threshold")),
        ("隐私级别",   _display_profile_value("privacy_level", explicit.get("privacy_level")), explicit.get("privacy_level")),
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
    from ..core.personalization.user_profile_store import format_inferred_preferences_display
    inferred_display = format_inferred_preferences_display(inferred)
    if inferred_display:
        lines.append(inferred_display)
    else:
        lines.append("- 暂无推断偏好（收到足够反馈后 Jarvis 会自动推断）")

    lines.extend(_format_feedback_summary(feedback))
    lines.extend([
        "",
        "说明：这里不会显示原始行为日志或短期 raw events。",
        "你可以使用 /update_profile 修改明确偏好；"
        "使用 /clear_inferred 仅清空推断偏好（保留明确设置）；"
        "使用 /delete_profile 删除全部个性化数据。",
    ])
    return "\n".join(lines)


def _delete_profile_requires_confirmation() -> bool:
    """Hook for adding a multi-step delete confirmation flow later."""
    return True


# ── Profile update / onboarding constants ─────────────────────────────────────

_PROFILE_FIELD_ALIASES: dict[str, str] = {
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

_PURPOSE_ALIASES: dict[str, str] = {
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

_STYLE_ALIASES: dict[str, str] = {
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

_PRIVACY_ALIASES: dict[str, str] = {
    "minimal": "minimal",
    "最少": "minimal",
    "最小": "minimal",
    "standard": "standard",
    "标准": "standard",
    "strict": "strict",
    "严格": "strict",
}

_ALERT_PREFERENCE_ALIASES: dict[str, str] = {
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

_UPDATE_PROFILE_LABELS: dict[str, str] = {
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

_UPDATE_PROFILE_VALUE_LABELS: dict[str, dict[str, str]] = {
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
_FEEDBACK_TOPIC_RE = re.compile(
    r"(?:^|\s)(?:topic|主题)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|(.+))$"
)

_UPDATE_PROFILE_ERROR_MESSAGES: dict[str, str] = {
    "empty":           "请在 /update_profile 后添加要更新的字段。",
    "bad_format":      "格式不正确，请使用 key=value，例如：目标汇率=4.85。",
    "target_rate":     "目标汇率必须是合理数字，例如 4.85。",
    "alert_threshold": "提醒阈值必须是 0 到 10 之间的百分比数字，例如 0.3 表示 0.3%。",
    "purpose":         "用途建议使用：学费、生活、投资、一般。",
    "style":           "风格可选：简短、普通、详细。",
    "topics":          "主题不能为空，可用逗号、中文逗号或顿号分隔，例如：RBA，oil，CNY。",
    "preferred_banks": "偏好银行可选：中国银行、工商银行、建设银行、农业银行、交通银行、招商银行、中信银行、兴业银行、光大银行、浦发银行。",
    "privacy_level":   "隐私级别可选：最少、标准、严格。",
    "alert_preference":"提醒偏好可选：目标汇率、波动率、重大新闻、晨报。",
}

_UPDATE_PROFILE_WIZARD_KEY  = "update_profile_wizard"
_DELETE_PROFILE_PENDING_KEY = "delete_profile_pending"
_UPDATE_PROFILE_WIZARD_STEPS: list[tuple[str, str]] = [
    ("target_rate",            "请输入目标汇率，例如：4.85"),
    ("alert_threshold",        "请输入提醒阈值百分比，例如：0.3 表示 0.3%"),
    ("purpose",                "请输入用途：学费、生活、投资、一般"),
    ("preferred_summary_style","请输入摘要风格：简短、普通、详细"),
    ("preferred_topics",       "请输入关注主题，可用逗号或顿号分隔，例如：RBA，oil，CNY"),
    ("preferred_banks",        "请输入偏好银行，可用逗号或顿号分隔，例如：中国银行，建设银行，工商银行"),
    ("language",               "请输入语言，例如：zh-CN 或 中文"),
    ("privacy_level",          "请输入隐私级别：最少、标准、严格"),
]

_ONBOARDING_KEY   = "profile_onboarding"
_ONBOARDING_STEPS: list[tuple[str, str]] = [
    ("purpose",                "你的主要用途是什么？\n请选择：学费 / 生活费 / 投资 / 其他"),
    ("alert_preference",       "你更希望 Jarvis 优先提醒什么？\n请选择：目标汇率 / 波动率 / 重大新闻 / 晨报"),
    ("preferred_summary_style","你喜欢哪种摘要风格？\n请选择：简短 / 普通 / 详细"),
]
_ONBOARDING_INTRO = (
    "为了让 Jarvis 的提醒更贴近你的使用方式，我会先问 3 个简单问题。\n"
    "不会询问余额、银行信息、证件号或地址。\n\n"
    "你也可以回复\"跳过引导\"，直接使用默认资料；跳过后不会反复询问。"
)


# ── Inline feedback button helpers ────────────────────────────────────────────

_FB_PREFIX = "fb:"

_FB_SOURCE_LABELS: dict[str, str] = {
    "research": "研究简报",
    "news":     "新闻推送",
    "morning":  "每日早报",
    "alert":    "联合告警",
}

_FEEDBACK_TYPE_ALIASES: dict[str, str] = {
    "useful":         "useful",
    "有用":           "useful",
    "not_useful":     "not_useful",
    "无用":           "not_useful",
    "useless":        "not_useful",
    "not_interested": "not_interested",
    "不感兴趣":       "not_interested",
}

_FEEDBACK_TYPE_LABELS: dict[str, str] = {
    "useful":         "有用",
    "not_useful":     "无用",
    "not_interested": "不感兴趣",
}


def _make_feedback_keyboard(source: str) -> InlineKeyboardMarkup:
    """Return a one-row inline keyboard with three feedback buttons."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👍 有用",    callback_data=f"{_FB_PREFIX}useful:{source}"),
        InlineKeyboardButton("👎 无用",    callback_data=f"{_FB_PREFIX}not_useful:{source}"),
        InlineKeyboardButton("🚫 不感兴趣", callback_data=f"{_FB_PREFIX}not_interested:{source}"),
    ]])


# ── Profile update parsers ────────────────────────────────────────────────────

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
            value.lower(), _ALERT_PREFERENCE_ALIASES.get(value)
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
    message = _UPDATE_PROFILE_ERROR_MESSAGES.get(
        code, _UPDATE_PROFILE_ERROR_MESSAGES["bad_format"]
    )
    return f"{message}\n\n{_UPDATE_PROFILE_USAGE}"


def _update_profile_prompt(step_index: int) -> str:
    _, prompt = _UPDATE_PROFILE_WIZARD_STEPS[step_index]
    return f"{prompt}\n\n输入\"跳过\"可跳过这一项，输入\"取消\"可退出修改。"


def _onboarding_prompt(step_index: int) -> str:
    _, prompt = _ONBOARDING_STEPS[step_index]
    return f"{prompt}\n\n回复\"跳过\"可跳过这一题，回复\"跳过引导\"可结束引导。"


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


# ── Feedback command parsers ──────────────────────────────────────────────────

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


# ── Phase 9 research helpers ──────────────────────────────────────────────────

def _ensure_research_path() -> None:
    """Add the Phase 9 research directory to sys.path (idempotent)."""
    import sys as _sys
    p = str(_RESEARCH_DIR)
    if p not in _sys.path:
        _sys.path.insert(0, p)


_SECTION_EMOJIS: dict[str, str] = {
    "汇率事实": "📈",
    "新闻驱动": "📰",
    "宏观信号": "🌐",
    "风险与矛盾": "⚠️",
}


def _split_brief_points(text: str, *, max_points: int = 10) -> list[str]:
    """Split dense research prose into readable Telegram bullet points."""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return []
    if cleaned.startswith(("•", "-", "1.")):
        return [line.strip() for line in cleaned.splitlines() if line.strip()]

    parts = re.split(r"(?<=[。！？!?])\s*|[；;]\s*", cleaned)
    points: list[str] = []
    for part in parts:
        item = part.strip(" ，,")
        if not item:
            continue
        if len(item) > 180:
            comma_parts = [
                p.strip(" ，,") for p in re.split(r"，|,\s+", item) if p.strip(" ，,")
            ]
            points.extend(comma_parts if len(comma_parts) > 1 else [item])
        else:
            points.append(item)
    return points[:max_points] or [cleaned]


def _format_brief_points(text: str, *, max_points: int = 10) -> str:
    """Render text as bullet points, preserving existing bullet-like lines."""
    points = _split_brief_points(text, max_points=max_points)
    if not points:
        return "（无内容）"
    rendered: list[str] = []
    for point in points:
        if point.startswith(("•", "-")):
            rendered.append(point)
        else:
            rendered.append(f"• {point}")
    return "\n".join(rendered)


def _format_research_brief(brief: Any, latency_s: float) -> str:
    """
    Render a ResearchBrief as a plain-text Telegram message.
    No LLM involved — pure string assembly from ResearchBrief fields.
    """
    lines: list[str] = [
        "📊 CNY/AUD 研究简报",
        "",
        "🔍 结论摘要",
        brief.conclusion or "（暂无结论）",
        "",
    ]

    for sec in brief.sections:
        emoji = _SECTION_EMOJIS.get(sec.title, "📌")
        header = f"{emoji} {sec.title}"
        if sec.has_data_gap:
            header += "  ⚠️ 数据不完整"
        lines.append(header)
        lines.append(_format_brief_points(sec.content, max_points=12))
        lines.append("")

    if brief.data_gaps:
        lines += ["📋 研究覆盖不足", _format_brief_points(brief.data_gaps, max_points=8), ""]

    if brief.user_notes:
        lines += ["👤 个性化备注", f"{brief.user_notes}（仅基于您的明确偏好）", ""]

    if brief.sources_summary:
        lines += ["📎 数据来源", brief.sources_summary, ""]

    c = brief.cost_estimate
    cost_str = f"~${c.estimated_cost_usd:.4f}" if c.estimated_cost_usd > 0 else "~$0.0000"
    lines.append(
        f"💰 本次研究成本：~{c.llm_calls} 次 LLM 调用 · "
        f"~{c.estimated_tokens:,} 个令牌 · {cost_str}"
    )
    lines.append(f"⏱ 总耗时：{latency_s:.1f}s")
    lines.append(f"🔖 简报 ID：{brief.task_id[:8]}")
    lines.append("")
    lines.append(f"{brief.disclaimer}")

    return "\n".join(lines)


# ── Message post-processing utilities ─────────────────────────────────────────

_LEAKED_TOOL_RE = re.compile(
    r'<\s*\|?\s*(?:DSML|antml)\s*\|\s*function_calls[^>]*>'
    r'[\s\S]*?'
    r'</\s*\|?\s*(?:DSML|antml)\s*\|\s*function_calls\s*>',
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
