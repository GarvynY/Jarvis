"""Tests for privacy-conscious FX research baseline recorder."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import baseline_recorder as br


class _Row(dict):
    def __getitem__(self, key: str):
        return self.get(key)


def _make_objects():
    task = SimpleNamespace(task_id="task-abc123", preset_name="fx_cnyaud")
    preset = SimpleNamespace(name="fx_cnyaud")
    out = SimpleNamespace(
        agent_name="fx_agent",
        status="ok",
        confidence=0.9,
        findings=[object()],
        sources=[object()],
        evidence_count=1,
        missing_data=[],
        latency_ms=123,
        token_usage={"prompt_tokens": 100, "completion_tokens": 20},
    )
    cost = SimpleNamespace(
        llm_calls=1,
        estimated_tokens=120,
        estimated_cost_usd=0.001,
        total_latency_ms=1000,
    )
    brief = SimpleNamespace(
        task_id="task-abc123",
        agent_statuses={"fx_agent": "ok"},
        data_gaps="",
        cost_estimate=cost,
    )
    return task, preset, [out], brief


def _fake_evidence_rows(_task_id: str):
    chunks = [
        _Row({
            "chunk_id": "chunk-1",
            "agent_name": "fx_agent",
            "category": "fx_price",
            "source": "url=https://www.reuters.com/markets/x?secret=1 | title=Example",
            "used_in_brief": 1,
            "composite_score": 0.8,
            "attention_score": 0.7,
            "score_source_quality": 0.95,
            "score_reason": "premium_source",
            "token_estimate": 42,
        })
    ]
    findings = [_Row({"finding_id": "finding-1"})]
    trace = SimpleNamespace(
        selected_chunk_ids=["chunk-1"],
        section_covered=True,
        retrieved_count=1,
        conflict_count=0,
        boosted_chunk_ids=[],
        fallback_reason="",
    )
    return chunks, findings, [trace]


def test_run_metrics_are_structured_and_private() -> None:
    task, preset, outputs, brief = _make_objects()
    original_loader = br._load_evidence_rows
    original_base_dir = br._base_dir
    with tempfile.TemporaryDirectory() as tmp:
        br._load_evidence_rows = _fake_evidence_rows
        br._base_dir = lambda: Path(tmp)
        try:
            result = br.record_fx_research_run(
                task=task,
                preset=preset,
                outputs=outputs,
                brief=brief,
                latency_s=1.25,
                trigger="test",
                user_id=123456789,
            )
            assert result.metrics_written is True
            assert result.baseline_written is False

            conn = sqlite3.connect(result.metrics_db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM fx_research_run_metrics WHERE task_id = ?",
                ("task-abc123",),
            ).fetchone()
            assert row is not None
            assert row["user_key"].startswith("sha256:")
            assert "123456789" not in json.dumps(dict(row), ensure_ascii=False)
            selected = json.loads(row["selected_chunk_metrics_json"])
            assert selected[0]["source_domain"] == "reuters.com"
            assert "secret=1" not in row["selected_chunk_metrics_json"]
            privacy = json.loads(row["privacy_policy_json"])
            assert privacy["prompt_text_stored"] is False
            assert privacy["evidence_text_stored"] is False
        finally:
            br._load_evidence_rows = original_loader
            br._base_dir = original_base_dir


def test_explicit_baseline_writes_sanitized_snapshot() -> None:
    task, preset, outputs, brief = _make_objects()
    original_loader = br._load_evidence_rows
    original_base_dir = br._base_dir
    with tempfile.TemporaryDirectory() as tmp:
        br._load_evidence_rows = _fake_evidence_rows
        br._base_dir = lambda: Path(tmp)
        try:
            result = br.record_fx_research_run(
                task=task,
                preset=preset,
                outputs=outputs,
                brief=brief,
                latency_s=1.25,
                trigger="test",
                user_id=123456789,
                record_baseline=True,
                phase10={
                    "ranking_basis": "composite_score",
                    "selected_chunks": [{"text_preview": "sensitive evidence text"}],
                    "score_summary": {"selected_count": 1},
                },
            )
            assert result.baseline_written is True
            snapshot = json.loads(Path(result.baseline_path).read_text(encoding="utf-8"))
            dumped = json.dumps(snapshot, ensure_ascii=False)
            assert "sensitive evidence text" not in dumped
            assert "selected_chunks" not in snapshot["phase10_sanitized"]
            assert snapshot["phase10_sanitized"]["ranking_basis"] == "composite_score"
        finally:
            br._load_evidence_rows = original_loader
            br._base_dir = original_base_dir


def run_all() -> None:
    tests = [
        test_run_metrics_are_structured_and_private,
        test_explicit_baseline_writes_sanitized_snapshot,
    ]
    print("FX baseline recorder tests")
    print("=" * 40)
    for test in tests:
        test()
        print(f"  {test.__name__} OK")
    print("=" * 40)
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    run_all()

