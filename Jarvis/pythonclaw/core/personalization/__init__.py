"""Structured personalization storage for Phase 8."""

from .user_profile_store import (
    ALLOWED_FEEDBACK_TOPICS,
    build_safe_user_context,
    clear_inferred_preferences,
    delete_user_profile,
    format_inferred_preferences_display,
    get_or_create_user,
    get_user_category_feedback_summary,
    get_user_profile,
    init_db,
    log_feedback_event,
    log_raw_event,
    mark_onboarding_completed,
    purge_expired_raw_events,
    update_explicit_preferences,
    update_inferred_preferences,
    update_inferred_preferences_from_feedback,
)

__all__ = [
    "ALLOWED_FEEDBACK_TOPICS",
    "build_safe_user_context",
    "clear_inferred_preferences",
    "delete_user_profile",
    "format_inferred_preferences_display",
    "get_or_create_user",
    "get_user_category_feedback_summary",
    "get_user_profile",
    "init_db",
    "log_feedback_event",
    "log_raw_event",
    "mark_onboarding_completed",
    "purge_expired_raw_events",
    "update_explicit_preferences",
    "update_inferred_preferences",
    "update_inferred_preferences_from_feedback",
]
