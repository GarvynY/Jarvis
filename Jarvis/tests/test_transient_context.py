from __future__ import annotations

import json
import sys
import types

openai_stub = types.ModuleType("openai")
openai_stub.OpenAI = object
sys.modules.setdefault("openai", openai_stub)

from pythonclaw.core.llm.base import LLMProvider
from pythonclaw.core.llm.response import MockChoice, MockMessage, MockResponse
from pythonclaw.core.persistent_agent import PersistentAgent


class FakeProvider(LLMProvider):
    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        return MockResponse(
            choices=[MockChoice(message=MockMessage(content="ok", tool_calls=None))]
        )

    def chat_stream(self, messages, tools=None, tool_choice="auto", **kwargs):
        yield {"type": "text_delta", "text": "ok"}
        return MockResponse(
            choices=[MockChoice(message=MockMessage(content="ok", tool_calls=None))]
        )


class FakeStore:
    def __init__(self):
        self.saved_messages = None

    def load(self, session_id):
        return []

    def save(self, session_id, messages):
        self.saved_messages = [dict(message) for message in messages]


def _agent(tmp_path, store):
    return PersistentAgent(
        provider=FakeProvider(),
        store=store,
        session_id="test-session",
        memory_dir=str(tmp_path / "memory"),
        skills_dirs=[],
        knowledge_path=None,
        persona_path="",
        soul_path="",
        tools_path="",
        auto_compaction=False,
    )


def _messages_text(messages) -> str:
    return json.dumps(messages, ensure_ascii=False, sort_keys=True)


def test_persistent_agent_chat_does_not_save_transient_system_context(tmp_path):
    store = FakeStore()
    agent = _agent(tmp_path, store)

    response = agent.chat("x", transient_system_context="secret")

    assert response == "ok"
    assert store.saved_messages is not None
    assert "secret" not in _messages_text(store.saved_messages)


def test_persistent_agent_chat_stream_does_not_save_transient_system_context(tmp_path):
    store = FakeStore()
    agent = _agent(tmp_path, store)

    response = agent.chat_stream("x", transient_system_context="secret")
    agent._save()

    assert response == "ok"
    assert store.saved_messages is not None
    assert "secret" not in _messages_text(store.saved_messages)
