"""
MemoryManager — long-term key-value memory with hybrid RAG recall.

Storage
-------
Memories are stored as Markdown files:
  - MEMORY.md        — curated long-term memory (latest value per key)
  - YYYY-MM-DD.md    — daily append-only log

When writing, both MEMORY.md and today's daily log are updated.
When reading, MEMORY.md is the source of truth (holds latest per key).
Conflict resolution: if the same key is written multiple times, the most
recent write wins (MEMORY.md is always overwritten with the latest value).

Per-group isolation
-------------------
When ``global_memory_dir`` is set, recall merges results from BOTH the
local (group-specific) memory AND the global (shared) memory.  Writes
always go to the local memory only.  This lets each Telegram/Discord/
WhatsApp group maintain private memories while still having access to
shared knowledge.

Recall
------
When a specific query is given, the manager converts every memory entry into a
short "chunk"  ("{key}: {value}")  and runs hybrid sparse + dense retrieval to
return the most relevant ones.  When the query is empty or "*", ALL memories
are returned (full-dump mode, used by compaction and legacy callers).
"""

from __future__ import annotations

import logging
import re

from ..retrieval.retriever import HybridRetriever
from .storage import MemoryStorage

logger = logging.getLogger(__name__)

_DUMP_TRIGGERS = {"", "*", "all", "everything"}
_DAILY_MEMORY_LOG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")

_SAFE_BOOT_CONTEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "Language": ("language", "preferred_language", "user_language"),
    "Tone": ("tone", "assistant_tone", "preferred_tone"),
    "Target rate": ("target_rate", "cny_aud_target_rate", "aud_cny_target_rate"),
    "Alert threshold": (
        "alert_threshold",
        "rate_alert_threshold",
        "exchange_rate_alert_threshold",
    ),
    "Report time": ("report_time", "daily_report_time", "scheduled_report_time"),
    "Risk preference": (
        "risk_preference",
        "risk_preference_label",
        "risk_profile",
    ),
    "Summary style": (
        "summary_style",
        "preferred_summary_style",
        "report_summary_style",
    ),
}


class MemoryManager:
    """
    Manages long-term memories stored as Markdown files.

    Parameters
    ----------
    memory_dir        : path to the local memory directory.
    global_memory_dir : optional path to a shared/global memory directory.
                        When set, recall() merges results from both local and
                        global stores.  Writes always go to local only.
    use_dense         : include embedding retrieval for recall (False by default
                        — BM25 alone is fast and sufficient for small corpora).
    """

    def __init__(
        self,
        memory_dir: str | None = None,
        global_memory_dir: str | None = None,
        use_dense: bool = False,
    ) -> None:
        import os

        if memory_dir is None:
            from ... import config as _cfg
            memory_dir = os.path.join(str(_cfg.PYTHONCLAW_HOME), "context", "memory")

        self.storage = MemoryStorage(memory_dir)
        self._global_storage: MemoryStorage | None = None
        if global_memory_dir and os.path.isdir(global_memory_dir):
            self._global_storage = MemoryStorage(global_memory_dir)
        self._use_dense = use_dense

    # ── Merged memories (local + global) ─────────────────────────────────────

    def _merged_memories(self) -> dict[str, str]:
        """Return local memories overlaid on global memories."""
        merged: dict[str, str] = {}
        if self._global_storage is not None:
            for k, v in self._global_storage.list_all().items():
                merged[f"[global] {k}"] = v
        merged.update(self.storage.list_all())
        return merged

    # ── Core operations ──────────────────────────────────────────────────────

    def remember(self, content: str, key: str | None = None) -> str:
        """Store *content* under *key* in local (group) memory."""
        if not key:
            raise ValueError("Key is required for memory storage.")
        self.storage.set(key, content)
        return f"Memory stored: [{key}] = {content}"

    def recall(self, query: str, top_k: int = 10) -> str:
        """
        Retrieve safe memories relevant to *query*.

        Searches both local and global memories when global_memory_dir is set.

        - If query is empty / "*" / "all" → returns safe personalization context.
        - Otherwise → runs hybrid BM25 (+ optional dense) retrieval and
          returns only hits whose keys are on the Phase 8 safe whitelist.
        """
        all_memories = self._merged_memories()
        if not all_memories:
            return "No memories found."

        if query.strip().lower() in _DUMP_TRIGGERS:
            return self.get_safe_boot_context() or "(no safe personalization context configured)"

        safe_keys = {
            key
            for keys in _SAFE_BOOT_CONTEXT_FIELDS.values()
            for key in keys
        }

        corpus = [
            {"source": k, "content": f"{k}: {v}"}
            for k, v in all_memories.items()
            if k in safe_keys or (k.startswith("[global] ") and k[9:] in safe_keys)
        ]
        if not corpus:
            return "(no safe personalization context configured)"

        retriever = HybridRetriever(
            provider=None,
            use_sparse=True,
            use_dense=self._use_dense,
            use_reranker=False,
        )
        retriever.fit(corpus)
        hits = retriever.retrieve(query, top_k=top_k)

        if not hits:
            logger.debug("[MemoryManager] No safe RAG hits for '%s'.", query)
            return "(no matching safe personalization context)"

        lines = [f"- {h['source']}: {h['content'].split(': ', 1)[-1]}" for h in hits]
        return "\n".join(lines)

    def forget(self, key: str) -> str:
        """Remove a memory entry by key from local memory."""
        if self.storage.get(key) is not None:
            self.storage.delete(key)
            return f"Forgot: {key}"
        return f"Nothing found for: {key}"

    def memory_get(self, path: str) -> str:
        """Return safe personalization context, not raw memory files.

        Phase 8 privacy forbids exposing MEMORY.md, daily memory change logs,
        raw events, or tool results to the LLM. Keep the method for tool
        compatibility, but make it a safe-context endpoint.
        """
        requested = (path or "safe_personalization_context").strip()
        if requested in {
            "safe",
            "safe_context",
            "safe_personalization_context",
            "personalization",
        }:
            return self.get_safe_boot_context() or "(no safe personalization context configured)"
        if _DAILY_MEMORY_LOG_RE.match(requested):
            return "Blocked: daily memory logs are raw change logs and are not exposed to LLM tools."
        return (
            "Blocked: raw legacy memory files are not exposed. "
            "Use memory_get('safe_personalization_context') instead."
        )

    def list_files(self) -> list[str]:
        """List safe memory tool targets only.

        Raw MEMORY.md and daily log filenames are intentionally hidden from
        LLM tools; only the safe summary endpoint is advertised.
        """
        return ["safe_personalization_context"]

    # ── Safe boot context (auto-injected at session start) ───────────────────

    @staticmethod
    def _lookup_safe_field(memories: dict[str, str], keys: tuple[str, ...]) -> str:
        """Return the first configured safe field, preferring local values."""
        for key in keys:
            value = memories.get(key)
            if value:
                return str(value).strip()
        for key in keys:
            value = memories.get(f"[global] {key}")
            if value:
                return str(value).strip()
        return ""

    def get_safe_boot_context(self, max_chars: int = 3000) -> str:
        """Return only the Phase 8 safe personalization context.

        Raw daily logs, raw interaction history, tool results, INDEX.md, and
        the full MEMORY.md are deliberately excluded. Those sources may contain
        sensitive behavior traces or unstructured LLM-generated memories, and
        Phase 8 personalization must expose only approved structured fields to
        model prompts.
        """
        memories = self._merged_memories()
        lines: list[str] = []
        for label, keys in _SAFE_BOOT_CONTEXT_FIELDS.items():
            value = self._lookup_safe_field(memories, keys)
            if value:
                lines.append(f"- **{label}**: {value}")

        if not lines:
            return ""

        context = "### Safe Personalization Context\n" + "\n".join(lines)
        if max_chars > 0 and len(context) > max_chars:
            return context[:max_chars].rstrip()
        return context

    def boot_context(self, max_chars: int = 3000) -> str:
        """Backward-compatible wrapper for safe startup context.

        Older code calls ``boot_context()`` during agent startup. Keep that API
        stable, but route it through the Phase 8 whitelist so raw logs and full
        free-form memory never enter the LLM prompt.
        """
        return self.get_safe_boot_context(max_chars=max_chars)

    # ── INDEX.md — curated system/config info ───────────────────────────────

    def read_index(self) -> str:
        """Read the INDEX.md curated system info file."""
        return self.storage.read_index()

    def write_index(self, content: str) -> str:
        """Write the INDEX.md curated system info file."""
        return self.storage.write_index(content)

    # ── Helpers used by compaction ───────────────────────────────────────────

    def list_all(self) -> dict:
        """Return the raw {key: value} dict (local only, used by compaction)."""
        return self.storage.list_all()
