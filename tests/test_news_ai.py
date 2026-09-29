import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import news_ai
from macro_sources import NewsItem

NOW = 1790640000.0


def item(index, importance=1, text=None):
    return NewsItem(f"n{index}", NOW - index * 60, text or f"美联储官员讲话 {index}", "", importance, "华尔街见闻")


def completion(content, total=900, hit=512):
    return {
        "choices": [{"message": {"content": content, "reasoning_content": "…"}}],
        "usage": {"prompt_tokens": 600, "completion_tokens": total - 600, "total_tokens": total,
                  "prompt_tokens_details": {"prompt_cache_hit_tokens": hit}},
    }


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class FakePost:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.bodies = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.bodies.append(json)
        self.url, self.headers, self.timeout = url, headers, timeout
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class AnalystTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "ai_cache.json")
        self.now = [NOW]

    def analyst(self, post, **config):
        analyst = news_ai.NewsAnalyst(self.path, http_post=post, clock=lambda: self.now[0])
        analyst.configure(news_ai.AiConfig(**{"enabled": True, "api_key": "sk-test", **config}))
        return analyst

    def test_new_headlines_are_tagged_once_with_high_thinking_by_default(self):
        post = FakePost(FakeResponse(completion('{"t":[[1,-1],[2,1],[3,0]]}')))
        analyst = self.analyst(post)
        items = [item(1, importance=2), item(2), item(3)]
        self.assertEqual(analyst.tag_headlines(items, "市场定价 10月加息 70%"), {"n1": -1, "n2": 1, "n3": 0})
        body = post.bodies[0]
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["messages"][0]["content"], news_ai.TAG_SYSTEM_PROMPT)
        self.assertIn("1|", body["messages"][1]["content"])
        self.assertEqual(post.headers["Authorization"], "Bearer sk-test")
        self.assertEqual(analyst.usage_today(), {"tokens": 900, "calls": 1, "cache_hit": 512})

        self.now[0] += news_ai.TAG_MIN_INTERVAL + 1
        self.assertEqual(analyst.tag_headlines(items, ""), {})
        self.assertEqual(len(post.bodies), 1)  # nothing new, no request

        reloaded = news_ai.NewsAnalyst(self.path, http_post=post, clock=lambda: self.now[0])
        self.assertEqual(reloaded.cached_tags(), {"n1": -1, "n2": 1, "n3": 0})

    def test_batches_are_capped_and_prioritize_important_recent_items(self):
        post = FakePost(FakeResponse(completion('{"t":[]}')))
        analyst = self.analyst(post)
        items = [item(index, importance=3 if index == 14 else 1, text="长" * 400) for index in range(15)]
        analyst.tag_headlines(items, "")
        lines = post.bodies[0]["messages"][1]["content"].splitlines()[2:]
        self.assertEqual(len(lines), news_ai.TAG_MAX_ITEMS)
        self.assertTrue(all(len(line) < news_ai.TAG_MAX_CHARS + 20 for line in lines))
        self.assertIn(news_ai.clip("长" * 400, news_ai.TAG_MAX_CHARS), lines[0])

    def test_effort_off_disables_thinking_and_pins_temperature(self):
        post = FakePost(FakeResponse(completion('{"t":[[1,0]]}')))
        self.analyst(post, tag_effort="off").tag_headlines([item(1)], "")
        body = post.bodies[0]
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["temperature"], 0)
        self.assertNotIn("reasoning_effort", body)
        self.assertLess(body["max_tokens"], 200)

    def test_minimum_interval_and_daily_budget_stop_requests(self):
        post = FakePost(FakeResponse(completion('{"t":[[1,0]]}', total=5000)))
        analyst = self.analyst(post, daily_token_budget=4000)
        analyst.tag_headlines([item(1)], "")
        self.assertFalse(analyst.tag_ready())  # interval
        self.now[0] += news_ai.TAG_MIN_INTERVAL + 1
        self.assertFalse(analyst.tag_ready())  # budget spent
        self.assertIn("已达今日上限", analyst.status_text())
        self.now[0] += 86400
        self.assertTrue(analyst.tag_ready())  # a new day resets usage

    def test_failures_fall_back_quietly_and_are_retried_later(self):
        post = FakePost(requests.Timeout("slow"), FakeResponse({}, status=500),
                        FakeResponse(completion("")), FakeResponse(completion('{"t":[[1,1]]}')))
        analyst = self.analyst(post)
        for expected_error in ("Timeout", "HTTP 500", "返回为空或不是 JSON"):
            self.assertEqual(analyst.tag_headlines([item(1)], ""), {})
            self.assertIn(expected_error, analyst.last_error)
            self.now[0] += news_ai.TAG_MIN_INTERVAL + 1
        self.assertEqual(analyst.tag_headlines([item(1)], ""), {"n1": 1})
        self.assertEqual(analyst.last_error, "")

    def test_json_mode_rejection_is_retried_once_without_it(self):
        post = FakePost(FakeResponse({"error": "bad"}, status=400), FakeResponse(completion('结果 {"t":[[1,1]]}')))
        self.assertEqual(self.analyst(post).tag_headlines([item(1)], ""), {"n1": 1})
        self.assertIn("response_format", post.bodies[0])
        self.assertNotIn("response_format", post.bodies[1])

    def test_invalid_pairs_are_ignored(self):
        post = FakePost(FakeResponse(completion('{"t":[[1,2],[9,1],[true,1],"x",[2,-1]]}')))
        self.assertEqual(self.analyst(post).tag_headlines([item(1), item(2)], ""), {"n2": -1})

    def test_deep_read_uses_thinking_and_respects_cooldowns(self):
        answer = json.dumps({"stance": "偏空", "summary": "加息预期升温压制金价", "drivers": ["利率预期上行", "美元走强"],
                             "risks": ["避险需求"]}, ensure_ascii=False)
        post = FakePost(FakeResponse(completion(answer)), FakeResponse(completion('{"stance":"看涨"}')))
        analyst = self.analyst(post, deep_effort="max")
        deep = analyst.deep_read("规则评分：偏空 -50", "数据公布：非农", expected_bps=17.0, score=-50)
        self.assertEqual((deep.stance, deep.trigger, deep.expected_bps), ("偏空", "数据公布：非农", 17.0))
        self.assertEqual(post.bodies[0]["reasoning_effort"], "max")
        self.assertEqual(post.bodies[0]["messages"][0]["content"], news_ai.DEEP_SYSTEM_PROMPT)
        self.assertFalse(analyst.deep_ready(manual=False))
        self.assertFalse(analyst.deep_ready(manual=True))
        self.now[0] += news_ai.DEEP_RELEASE_GAP
        self.assertTrue(analyst.deep_ready(release=True))
        self.assertFalse(analyst.deep_ready())
        self.now[0] -= news_ai.DEEP_RELEASE_GAP
        self.assertEqual(analyst.manual_deep_wait(), f"冷却中 {news_ai.DEEP_MANUAL_COOLDOWN}s")
        self.now[0] += news_ai.DEEP_MANUAL_COOLDOWN + 1
        self.assertTrue(analyst.deep_ready(manual=True))
        self.assertEqual(analyst.manual_deep_wait(), "")
        self.assertIsNone(analyst.deep_read("brief", "手动请求", manual=True))
        self.assertEqual(analyst.last_error, "解读格式无效")
        self.assertEqual(analyst.last_deep.summary, "加息预期升温压制金价")
        reloaded = news_ai.NewsAnalyst(self.path, clock=lambda: self.now[0])
        self.assertEqual(reloaded.last_deep.drivers, ["利率预期上行", "美元走强"])

    def test_inactive_analyst_never_calls_the_api(self):
        post = FakePost()
        analyst = news_ai.NewsAnalyst(self.path, http_post=post, clock=lambda: self.now[0])
        analyst.configure(news_ai.AiConfig(enabled=True, api_key=""))
        self.assertEqual(analyst.tag_headlines([item(1)], ""), {})
        self.assertIsNone(analyst.deep_read("brief", "手动请求", manual=True))
        self.assertIn("未配置", analyst.status_text())
        self.assertEqual(post.bodies, [])

    def test_corrupt_cache_is_ignored(self):
        Path(self.path).write_text('{"tags": {"a": [5, 1], "b": [1, "x"], "c": [-1, 10]}, "usage": []}', encoding="utf-8")
        analyst = news_ai.NewsAnalyst(self.path)
        self.assertEqual(analyst.cached_tags(), {"c": -1})

    def test_environment_key_wins_and_fenced_json_is_accepted(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": " sk-env "}):
            self.assertEqual(news_ai.resolve_api_key("sk-config"), "sk-env")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(news_ai.resolve_api_key(" sk-config "), "sk-config")
        self.assertEqual(news_ai.parse_json_content('```json\n{"t": []}\n```'), {"t": []})
        self.assertEqual(news_ai.parse_json_content('结果：{"t": [[1, 0]]}'), {"t": [[1, 0]]})
        self.assertIsNone(news_ai.parse_json_content("[1, 2]"))


if __name__ == "__main__":
    unittest.main()
