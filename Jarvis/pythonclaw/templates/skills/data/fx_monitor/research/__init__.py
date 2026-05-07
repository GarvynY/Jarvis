"""
Phase 9 — Generic preset-driven research workflow engine.

Public surface of this package:

    Schema types:
        SafeUserContext, ResearchPreset, ResearchTask,
        SourceRef, Finding, AgentOutput,
        ResearchSection, CostEstimate, ResearchBrief

    Preset registry:
        PRESET_REGISTRY, FX_CNYAUD_PRESET

    Helpers:
        now_iso, to_dict, to_json,
        validate_status, validate_confidence

    Structured LLM output:
        StructuredLLMResult, call_json_with_repair, parse_json_object

Note: AGENT_REGISTRY is NOT exported from here.
It is owned by the coordinator module, which maps agent-name
strings to callable agents at runtime.
"""

from .schema import (
    # helpers
    now_iso,
    to_dict,
    to_json,
    validate_status,
    validate_confidence,
    # dataclasses
    SafeUserContext,
    ResearchPreset,
    ResearchTask,
    SourceRef,
    Finding,
    AgentOutput,
    ResearchSection,
    CostEstimate,
    ResearchBrief,
    # registries
    FX_CNYAUD_PRESET,
    PRESET_REGISTRY,
)
from .structured_llm import (
    StructuredLLMResult,
    call_json_with_repair,
    parse_json_object,
)

__all__ = [
    "now_iso",
    "to_dict",
    "to_json",
    "validate_status",
    "validate_confidence",
    "SafeUserContext",
    "ResearchPreset",
    "ResearchTask",
    "SourceRef",
    "Finding",
    "AgentOutput",
    "ResearchSection",
    "CostEstimate",
    "ResearchBrief",
    "FX_CNYAUD_PRESET",
    "PRESET_REGISTRY",
    "StructuredLLMResult",
    "call_json_with_repair",
    "parse_json_object",
]
