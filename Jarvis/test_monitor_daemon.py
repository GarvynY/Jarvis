import json
import unittest
from unittest.mock import patch

import monitor_daemon as md


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        return _FakeResponse(self.outputs.pop(0))


class _FakeChat:
    def __init__(self, outputs: list[str]):
        self.completions = _FakeCompletions(outputs)


class _FakeClient:
    def __init__(self, outputs: list[str]):
        self.chat = _FakeChat(outputs)


class MonitorDaemonNewsAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.articles = [
            {"title": "RBA signals higher rates could support AUD"},
            {"title": "Local sports result unrelated to FX"},
        ]

    def test_parse_json_returns_relevant_articles_with_tags(self):
        raw = json.dumps({
            "articles": [
                {
                    "index": 1,
                    "relevant": True,
                    "summary": "RBA 利率信号可能支撑澳元，1 AUD = X CNY 口径下 AUD 偏强。",
                    "tags": ["RBA利率", "澳元走强", "RBA利率"],
                },
                {"index": 2, "relevant": False, "summary": "", "tags": []},
            ]
        })

        parsed = md._parse_llm_article_analysis(raw, self.articles)

        self.assertEqual(len(parsed), 1)
        article, summary, tags = parsed[0]
        self.assertEqual(article["title"], self.articles[0]["title"])
        self.assertEqual(article["_llm_tags"], ["RBA利率", "澳元走强"])
        self.assertIn("AUD", summary)
        self.assertEqual(tags, ["RBA利率", "澳元走强"])

    def test_tags_are_length_limited_filtered_and_deduplicated(self):
        raw = json.dumps({
            "articles": [
                {
                    "index": 1,
                    "relevant": True,
                    "summary": "短期利率预期变化影响澳元。",
                    "tags": [
                        "RBA利率!!!!",
                        "rba 利率",
                        "verylongtagvalue-over-limit",
                        "token=secret123",
                        "user@example.com",
                    ],
                },
                {"index": 2, "relevant": False, "summary": "", "tags": []},
            ]
        })

        _article, _summary, tags = md._parse_llm_article_analysis(raw, self.articles)[0]

        self.assertLessEqual(len(tags), md._MAX_NEWS_TAGS)
        self.assertIn("RBA利率", tags)
        self.assertNotIn("token=secret123", tags)
        self.assertTrue(all(len(tag) <= md._MAX_NEWS_TAG_CHARS for tag in tags))
        self.assertEqual(len(tags), len({md._normalize_tag_key(tag) for tag in tags}))

    def test_invalid_json_raises_validation_error(self):
        with self.assertRaises(json.JSONDecodeError):
            md._parse_llm_article_analysis("1. RBA 利率相关", self.articles)

    def test_relevant_article_requires_valid_tags(self):
        raw = json.dumps({
            "articles": [
                {"index": 1, "relevant": True, "summary": "RBA 影响澳元。", "tags": []},
                {"index": 2, "relevant": False, "summary": "", "tags": []},
            ]
        })

        with self.assertRaises(ValueError):
            md._parse_llm_article_analysis(raw, self.articles)

    def test_json_must_cover_each_article_index(self):
        raw = json.dumps({
            "articles": [
                {
                    "index": 1,
                    "relevant": True,
                    "summary": "RBA 利率信号可能支撑澳元。",
                    "tags": ["RBA利率"],
                },
            ]
        })

        with self.assertRaises(ValueError):
            md._parse_llm_article_analysis(raw, self.articles)

    def test_llm_analysis_retries_after_invalid_output(self):
        invalid = "1. RBA 利率相关"
        valid = json.dumps({
            "articles": [
                {
                    "index": 1,
                    "relevant": True,
                    "summary": "RBA 利率信号可能支撑澳元。",
                    "tags": ["RBA利率", "澳元"],
                },
                {"index": 2, "relevant": False, "summary": "", "tags": []},
            ]
        })
        client = _FakeClient([invalid, valid])

        with patch.object(md, "_make_deepseek_client", return_value=client):
            result = md._llm_per_article_analysis("fake-key", self.articles)

        self.assertEqual(client.chat.completions.calls, 2)
        self.assertEqual(result[0][2], ["RBA利率", "澳元"])

    def test_news_feedback_keyboard_uses_short_tokens(self):
        keyboard = md._news_feedback_keyboard([
            ("RBA利率", 123, 0),
            ("澳元走强", 123, 1),
            ("能源风险", 456, 0),
        ])

        rows = keyboard["inline_keyboard"]
        callback_values = [
            button["callback_data"]
            for row in rows
            for button in row
        ]

        self.assertIn("nf:u:123:0", callback_values)
        self.assertIn("nf:ni:123", callback_values)
        self.assertTrue(all(len(value.encode("utf-8")) <= 64 for value in callback_values))

    def test_store_news_feedback_contexts_uses_one_push_context(self):
        calls = []

        def fake_store(_chat_id, **kwargs):
            calls.append(kwargs)
            return 77

        relevant = [
            ({"title": "RBA rates", "url": "https://example.com/1"}, "RBA summary", ["RBA利率", "澳元"]),
            ({"title": "Energy risk", "url": "https://example.com/2"}, "Energy summary", ["能源风险", "澳元"]),
        ]

        with patch.dict(
            "sys.modules",
            {
                "pythonclaw.core.personalization": type(
                    "_P",
                    (),
                    {
                        "store_news_feedback_context": staticmethod(fake_store),
                        "get_news_feedback_rollup_status": staticmethod(
                            lambda _chat_id: {"feedback_count": 0, "threshold_reached": False}
                        ),
                    },
                ),
            },
        ):
            buttons = md._store_news_feedback_contexts(123, relevant)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tags"], ["RBA利率", "澳元", "能源风险"])
        self.assertEqual(calls[0]["articles"][0]["title"], "RBA rates")
        self.assertEqual(calls[0]["articles"][0]["summary"], "RBA summary")
        self.assertEqual(calls[0]["articles"][1]["tags"], ["能源风险", "澳元"])
        self.assertEqual(calls[0]["metadata"]["scope"], "news_push")
        self.assertEqual(buttons, [("RBA利率", 77, 0), ("澳元", 77, 1), ("能源风险", 77, 2)])

    def test_preference_agent_check_runs_for_realtime_news_users_only(self):
        calls = []

        class _Result:
            ok = True
            declarations_created = 1
            context_ids_summarized = ["77"]
            attempts = 1
            error = ""

        def fake_run(user_id):
            calls.append(user_id)
            return _Result()

        with patch.dict(
            "sys.modules",
            {
                "pythonclaw.core.personalization": type(
                    "_P",
                    (),
                    {"run_preference_agent_for_user": staticmethod(fake_run)},
                ),
            },
        ):
            md.check_news_feedback_preferences([123, "456", "bad"])

        self.assertEqual(calls, [123, 456])

    def test_preference_agent_check_unavailable_is_nonfatal(self):
        with patch.dict("sys.modules", {"pythonclaw.core.personalization": object()}):
            md.check_news_feedback_preferences(123)


if __name__ == "__main__":
    unittest.main()
