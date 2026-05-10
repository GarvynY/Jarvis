"""
Phase 10C — Rule-based Conflict Detector.

Detects directional contradictions between findings within the same
research task. Operates on EvidenceFinding objects (which carry direction).

MVP conflict rules:
  1. Same category + opposite direction (bullish_aud vs bearish_aud)
  2. Same entity + opposite direction (via chunk entity overlap)

No LLM calls, no external APIs, no Telegram modifications.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)

try:
    from schema import EvidenceFinding
except ImportError:
    from .schema import EvidenceFinding  # type: ignore[no-redef]


# ── Direction opposition map ────────────────────────────────────────────────

_OPPOSITE_DIRECTIONS: dict[str, str] = {
    "bullish_aud": "bearish_aud",
    "bearish_aud": "bullish_aud",
}


def _are_opposed(d1: str | None, d2: str | None) -> bool:
    """Return True if two directions are in direct opposition."""
    if not d1 or not d2:
        return False
    return _OPPOSITE_DIRECTIONS.get(d1) == d2


# ── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass
class ConflictPair:
    """One detected conflict between two findings."""
    finding_id_a: str
    finding_id_b: str
    chunk_id_a: str = ""
    chunk_id_b: str = ""
    category: str = ""
    direction_a: str = ""
    direction_b: str = ""
    rule: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id_a": self.finding_id_a,
            "finding_id_b": self.finding_id_b,
            "chunk_id_a": self.chunk_id_a,
            "chunk_id_b": self.chunk_id_b,
            "category": self.category,
            "direction_a": self.direction_a,
            "direction_b": self.direction_b,
            "rule": self.rule,
            "confidence": self.confidence,
        }


@dataclass
class ConflictSummary:
    """Result of conflict detection on a set of findings."""
    conflicts: list[ConflictPair] = field(default_factory=list)
    conflict_count: int = 0
    conflicting_chunk_ids: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflicts": [c.to_dict() for c in self.conflicts],
            "conflict_count": self.conflict_count,
            "conflicting_chunk_ids": sorted(self.conflicting_chunk_ids),
        }


# ── Core detection ──────────────────────────────────────────────────────────

def detect_conflicts(
    findings: list[EvidenceFinding],
    *,
    chunk_entities: dict[str, list[str]] | None = None,
) -> ConflictSummary:
    """Detect directional contradictions among findings.

    Args:
        findings: list of EvidenceFinding objects with direction fields.
        chunk_entities: optional mapping chunk_id -> entity list for
            entity-overlap conflict detection.

    Returns:
        ConflictSummary with all detected conflict pairs.
    """
    if not findings or len(findings) < 2:
        return ConflictSummary()

    directed = [f for f in findings if f.direction and f.direction not in ("neutral", "mixed", "unknown")]
    if len(directed) < 2:
        return ConflictSummary()

    conflicts: list[ConflictPair] = []
    seen_pairs: set[tuple[str, str]] = set()

    for i, fa in enumerate(directed):
        for fb in directed[i + 1:]:
            pair_key = (fa.finding_id, fb.finding_id)
            if pair_key in seen_pairs:
                continue

            if not _are_opposed(fa.direction, fb.direction):
                continue

            # Rule 1: same category + opposite direction
            if fa.category and fa.category == fb.category:
                chunk_a = fa.chunk_ids[0] if fa.chunk_ids else ""
                chunk_b = fb.chunk_ids[0] if fb.chunk_ids else ""
                conflicts.append(ConflictPair(
                    finding_id_a=fa.finding_id,
                    finding_id_b=fb.finding_id,
                    chunk_id_a=chunk_a,
                    chunk_id_b=chunk_b,
                    category=fa.category,
                    direction_a=fa.direction or "",
                    direction_b=fb.direction or "",
                    rule="same_category_opposite_direction",
                    confidence=0.9,
                ))
                seen_pairs.add(pair_key)
                continue

            # Rule 2: shared entity + opposite direction
            if chunk_entities:
                ents_a: set[str] = set()
                for cid in fa.chunk_ids:
                    ents_a.update(chunk_entities.get(cid, []))
                ents_b: set[str] = set()
                for cid in fb.chunk_ids:
                    ents_b.update(chunk_entities.get(cid, []))
                if ents_a & ents_b:
                    chunk_a = fa.chunk_ids[0] if fa.chunk_ids else ""
                    chunk_b = fb.chunk_ids[0] if fb.chunk_ids else ""
                    conflicts.append(ConflictPair(
                        finding_id_a=fa.finding_id,
                        finding_id_b=fb.finding_id,
                        chunk_id_a=chunk_a,
                        chunk_id_b=chunk_b,
                        category=fa.category or fb.category,
                        direction_a=fa.direction or "",
                        direction_b=fb.direction or "",
                        rule="shared_entity_opposite_direction",
                        confidence=0.7,
                    ))
                    seen_pairs.add(pair_key)

    conflicting_ids: set[str] = set()
    for cp in conflicts:
        if cp.chunk_id_a:
            conflicting_ids.add(cp.chunk_id_a)
        if cp.chunk_id_b:
            conflicting_ids.add(cp.chunk_id_b)

    return ConflictSummary(
        conflicts=conflicts,
        conflict_count=len(conflicts),
        conflicting_chunk_ids=conflicting_ids,
    )


# ── Conflict value boost ───────────────────────────────────────────────────

CONFLICT_BOOST: float = 0.15

def apply_conflict_boost(
    score_map: dict[str, float],
    summary: ConflictSummary,
    *,
    boost: float = CONFLICT_BOOST,
) -> dict[str, float]:
    """Boost composite scores for chunks involved in conflicts.

    Conflicting evidence deserves higher visibility so the supervisor
    can present both sides to the user.

    Modifies score_map in-place and returns it.
    """
    for chunk_id in summary.conflicting_chunk_ids:
        if chunk_id in score_map:
            old = score_map[chunk_id]
            score_map[chunk_id] = min(1.0, round(old + boost, 4))
            _log.debug(
                "conflict_boost: %s %.4f -> %.4f",
                chunk_id, old, score_map[chunk_id],
            )
    return score_map
