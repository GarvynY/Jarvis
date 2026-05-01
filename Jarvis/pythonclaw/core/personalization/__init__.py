"""Structured personalization storage for Phase 8."""

from .user_profile_store import (
    build_safe_user_context,
    delete_user_profile,
    get_or_create_user,
    get_user_profile,
    init_db,
    log_feedback_event,
    log_raw_event,
    mark_onboarding_completed,
    purge_expired_raw_events,
    update_explicit_preferences,
    update_inferred_preferences,
)

__all__ = [
    "build_safe_user_context",
    "delete_user_profile",
    "get_or_create_user",
    "get_user_profile",
    "init_db",
    "log_feedback_event",
    "log_raw_event",
    "mark_onboarding_completed",
    "purge_expired_raw_events",
    "update_explicit_preferences",
    "update_inferred_preferences",
]
