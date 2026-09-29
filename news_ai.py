"""Optional DeepSeek reading of Fed/gold headlines.

Two tiers:

* ``tag_headlines``: hawkish/dovish tags for *new* relevant headlines only.
  Every headline id is sent at most once and its tag is cached on disk, so
  restarts never pay twice and each request stays short.
* ``deep_read``: an occasional synthesis of the whole scorecard. It runs on
  key releases, big shifts in rate pricing, a flipped stance, a twice-daily
  refresh or an explicit request from the outlook panel.

Both tiers default to thinking mode with ``reasoning_effort=high``; each can be
turned down (``off`` disables thinking) in the settings. Network calls never
hold the state lock, so the UI can read status while a request is running.
Any failure returns nothing and the caller keeps the rule-based lexicon.
"""

import json
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence

import requests

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
EFFORTS = ("off", "low", "high", "max")

TAG_MAX_ITEMS = 10
TAG_MAX_CHARS = 200
TAG_MIN_INTERVAL = 180
DEEP_MIN_INTERVAL = 1800
DEEP_RELEASE_GAP = 300  # a key print is worth a read soon after an earlier one
DEEP_MANUAL_COOLDOWN = 60
DEEP_BRIEF_CHARS = 3000
CACHE_RETENTION = 3 * 86400

# Fixed system prompts come first in every request so DeepSeek's prefix cache
# can serve them at the cache-hit rate. Keep them byte-for-byte stable.
TAG_SYSTEM_PROMPT = (
    "你是贵金属宏观分析师。逐条判断快讯对黄金价格未来1-5个交易日的影响方向，"
    "只考虑美联储利率路径、实际利率、美元和避险需求。\n"
    "打分：1=利多黄金（降息或暂停加息预期升温、通胀或就业走弱、美元或美债收益率走弱、避险升温）；"
    "-1=利空黄金（加息或推迟降息预期升温、通胀或就业走强、美元或美债收益率走强）；0=无关或方向不明。\n"
    "只依据快讯文字本身，不要推测。输出 json，覆盖每个编号，例如：\n"
    '{"t":[[1,-1],[2,0],[3,1]]}'
)
DEEP_SYSTEM_PROMPT = (
    "你是贵金属宏观策略师。根据给定的评分快照、最新数据、事件和要闻，判断黄金未来1-5个交易日的方向。\n"
    "要求：结论只能来自输入信息；给出最关键的2-3个驱动和1-2个风险点，每条不超过30字；summary不超过80字。\n"
    "stance 只能是 偏多、偏空、中性 之一。输出 json，例如：\n"
    '{"stance":"偏空","summary":"……","drivers":["……","……"],"risks":["……"]}'
)


@dataclass
class AiConfig:
    enabled: bool = False
    api_key: str = ""
    model: str = DEFAULT_MODEL
    daily_token_budget: int = 0          # 0 = unlimited
    tag_effort: str = "high"
    deep_effort: str = "high"

    @property
    def active(self):
        # type: () -> bool
        return self.enabled and bool(self.api_key.strip())


@dataclass
class DeepRead:
    at: float
    stance: str
    summary: str
    drivers: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    trigger: str = ""
    model: str = ""
    expected_bps: Optional[float] = None
    score: Optional[int] = None


def resolve_api_key(configured):
    # type: (str) -> str
    """The environment wins so a key never has to live in config.json."""
    return os.environ.get("DEEPSEEK_API_KEY", "").strip() or (configured or "").strip()


def clip(text, limit):
    # type: (str, int) -> str
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_json_content(content):
    # type: (Optional[str]) -> Optional[dict]
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    for candidate in (text, (re.search(r"\{.*\}", text, re.S) or [None])[0]):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _request_limits(effort, thinking_tokens, plain_tokens, thinking_timeout, plain_timeout):
    if effort == "off":
        return plain_tokens, plain_timeout
    return thinking_tokens, thinking_timeout


class NewsAnalyst:
    """DeepSeek client with per-headline caching and optional daily budget."""

    def __init__(self, cache_path, http_post=None, clock=time.time):
        # type: (str, Optional[Callable], Callable[[], float]) -> None
        self._cache_path = cache_path
        self._post = http_post or requests.post
        self._clock = clock
        self._lock = threading.RLock()
        self._busy = False
        self.config = AiConfig()
        self._tags = {}  # type: Dict[str, List[float]]
        self._usage = {"day": "", "tokens": 0, "calls": 0, "cache_hit": 0}
        self._last_tag_call = 0.0
        self._last_deep_call = 0.0
        self.last_deep = None  # type: Optional[DeepRead]
        self.last_error = ""
        self._load()

    # ------------------------------------------------------------------ state
    def configure(self, config):
        # type: (AiConfig) -> None
        with self._lock:
            self.config = AiConfig(
                enabled=bool(config.enabled),
                api_key=(config.api_key or "").strip(),
                model=(config.model or "").strip() or DEFAULT_MODEL,
                daily_token_budget=max(0, int(config.daily_token_budget)),
                tag_effort=config.tag_effort if config.tag_effort in EFFORTS else "high",
                deep_effort=config.deep_effort if config.deep_effort in EFFORTS else "high",
            )

    @property
    def active(self):
        # type: () -> bool
        return self.config.active

    def cached_tags(self):
        # type: () -> Dict[str, int]
        with self._lock:
            return {key: int(value[0]) for key, value in self._tags.items()}

    def usage_today(self):
        # type: () -> Dict[str, int]
        with self._lock:
            self._roll_day()
            return {key: int(value) for key, value in self._usage.items() if key != "day"}

    def status_text(self):
        # type: () -> str
        config = self.config
        if not config.enabled:
            return "AI 解读未开启 · 使用规则词典"
        if not config.api_key:
            return "未配置 DeepSeek Key · 使用规则词典"
        usage = self.usage_today()
        text = f"{config.model} · 今日 {usage['tokens'] / 1000:.1f}K tokens / {usage['calls']} 次"
        if config.daily_token_budget and usage["tokens"] >= config.daily_token_budget:
            text += " · 已达今日上限"
        elif self._busy:
            text += " · 分析中…"
        elif self.last_error:
            text += f" · 上次失败：{self.last_error}"
        return text

    def pending(self, items):
        # type: (Sequence) -> List
        """Headlines not tagged yet, most important and newest first."""
        with self._lock:
            fresh = [item for item in items if item.id not in self._tags]
        return sorted(fresh, key=lambda item: (-item.importance, -item.ts))

    def tag_ready(self):
        # type: () -> bool
        with self._lock:
            return (self.active and not self._busy and self._within_budget()
                    and self._clock() - self._last_tag_call >= TAG_MIN_INTERVAL)

    def deep_ready(self, manual=False, release=False):
        # type: (bool, bool) -> bool
        with self._lock:
            gap = DEEP_MANUAL_COOLDOWN if manual else DEEP_RELEASE_GAP if release else DEEP_MIN_INTERVAL
            return (self.active and not self._busy and self._within_budget()
                    and self._clock() - self._last_deep_call >= gap)

    def manual_deep_wait(self):
        # type: () -> str
        """Why a manual deep read cannot start right now; empty when it can."""
        with self._lock:
            if self._busy:
                return "AI 忙"
            if not self._within_budget():
                return "已达今日上限"
            left = DEEP_MANUAL_COOLDOWN - (self._clock() - self._last_deep_call)
            return f"冷却中 {math.ceil(left)}s" if left > 0 else ""

    def _roll_day(self):
        today = datetime.fromtimestamp(self._clock()).strftime("%Y-%m-%d")
        if self._usage.get("day") != today:
            self._usage = {"day": today, "tokens": 0, "calls": 0, "cache_hit": 0}

    def _within_budget(self):
        # type: () -> bool
        self._roll_day()
        budget = self.config.daily_token_budget
        return budget <= 0 or self._usage["tokens"] < budget

    # -------------------------------------------------------------- requests
    def tag_headlines(self, items, context):
        # type: (Sequence, str) -> Dict[str, int]
        """Tag up to ``TAG_MAX_ITEMS`` unseen headlines. Returns the new tags."""
        if not self.tag_ready():
            return {}
        batch = self.pending(items)[:TAG_MAX_ITEMS]
        if not batch:
            return {}
        lines = [
            f"{index}|{datetime.fromtimestamp(item.ts).strftime('%m-%d %H:%M')} {clip(item.text, TAG_MAX_CHARS)}"
            for index, item in enumerate(batch, start=1)
        ]
        user = f"背景：{clip(context, 120)}\n快讯：\n" + "\n".join(lines)
        effort = self.config.tag_effort
        max_tokens, timeout = _request_limits(effort, 8000, 60 + 12 * len(batch), (5, 150), (5, 30))
        with self._lock:
            self._last_tag_call = self._clock()
        data = self._request(TAG_SYSTEM_PROMPT, user, max_tokens, effort, timeout)
        tags = {}
        pairs = data.get("t") if data else None
        for pair in pairs if isinstance(pairs, list) else ():
            if (isinstance(pair, (list, tuple)) and len(pair) == 2
                    and all(isinstance(v, int) and not isinstance(v, bool) for v in pair)
                    and 1 <= pair[0] <= len(batch) and pair[1] in (-1, 0, 1)):
                tags[batch[pair[0] - 1].id] = pair[1]
        with self._lock:
            now = self._clock()
            for key, tag in tags.items():
                self._tags[key] = [tag, now]
            self._prune(now)
            self._save()
        return tags

    def deep_read(self, brief, trigger, manual=False, expected_bps=None, score=None, release=False):
        # type: (str, str, bool, Optional[float], Optional[int], bool) -> Optional[DeepRead]
        if not self.deep_ready(manual, release):
            return None
        with self._lock:
            self._last_deep_call = self._clock()
        effort = self.config.deep_effort
        max_tokens, timeout = _request_limits(effort, 16000, 1200, (5, 300), (5, 60))
        brief = brief.strip()
        if len(brief) > DEEP_BRIEF_CHARS:
            brief = brief[: DEEP_BRIEF_CHARS - 1] + "…"
        data = self._request(DEEP_SYSTEM_PROMPT, brief, max_tokens, effort, timeout)
        with self._lock:
            if not data or data.get("stance") not in ("偏多", "偏空", "中性"):
                if data is not None:
                    self.last_error = "解读格式无效"
                self._save()
                return None
            self.last_deep = DeepRead(
                at=self._clock(),
                stance=data["stance"],
                summary=clip(str(data.get("summary", "")), 100),
                drivers=[clip(v, 40) for v in data.get("drivers", []) if isinstance(v, str)][:3],
                risks=[clip(v, 40) for v in data.get("risks", []) if isinstance(v, str)][:2],
                trigger=trigger,
                model=self.config.model,
                expected_bps=expected_bps,
                score=score,
            )
            self._save()
            return self.last_deep

    def _request(self, system, user, max_tokens, effort, timeout):
        # type: (str, str, int, str, tuple) -> Optional[dict]
        config = self.config
        body = {
            "model": config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            "stream": False,
        }
        if effort == "off":
            body["thinking"] = {"type": "disabled"}
            body["temperature"] = 0
        else:
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = effort
        with self._lock:
            self._busy = True
        headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
        try:
            response = self._post(DEEPSEEK_URL, json=body, timeout=timeout, headers=headers)
            status = getattr(response, "status_code", 200)
            if status == 400:
                # Some model/mode combinations reject JSON mode; the prompt still asks for json.
                body = {key: value for key, value in body.items() if key != "response_format"}
                response = self._post(DEEPSEEK_URL, json=body, timeout=timeout, headers=headers)
                status = getattr(response, "status_code", 200)
            payload = response.json() if status < 400 else None
        except (requests.RequestException, ValueError) as exc:
            with self._lock:
                self.last_error = exc.__class__.__name__
            return None
        finally:
            with self._lock:
                self._busy = False
        with self._lock:
            if payload is None:
                self.last_error = f"HTTP {status}"
                return None
            self._record_usage(payload.get("usage") if isinstance(payload, dict) else None)
            try:
                content = payload["choices"][0]["message"].get("content")
            except (KeyError, IndexError, TypeError, AttributeError):
                self.last_error = "返回格式异常"
                return None
            data = parse_json_content(content)
            self.last_error = "" if data is not None else "返回为空或不是 JSON"
            return data

    def _record_usage(self, usage):
        # type: (Optional[dict]) -> None
        self._roll_day()
        self._usage["calls"] += 1
        if not isinstance(usage, dict):
            return
        total = usage.get("total_tokens")
        if not isinstance(total, int):
            total = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details")
        hit = (details or {}).get("prompt_cache_hit_tokens") if isinstance(details, dict) else None
        if hit is None:
            hit = usage.get("prompt_cache_hit_tokens", 0)
        self._usage["tokens"] += max(0, total)
        self._usage["cache_hit"] += int(hit) if isinstance(hit, (int, float)) else 0

    # ----------------------------------------------------------- persistence
    def _prune(self, now):
        cutoff = now - CACHE_RETENTION
        self._tags = {key: value for key, value in self._tags.items() if value[1] >= cutoff}

    def _load(self):
        try:
            with open(self._cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, UnicodeError):
            return
        if not isinstance(data, dict):
            return
        tags = data.get("tags")
        if isinstance(tags, dict):
            self._tags = {
                str(key): [int(value[0]), float(value[1])] for key, value in tags.items()
                if isinstance(value, list) and len(value) == 2 and value[0] in (-1, 0, 1)
                and isinstance(value[1], (int, float)) and not isinstance(value[1], bool)
            }
        usage = data.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("day"), str):
            try:
                self._usage = {"day": usage["day"], **{
                    key: int(usage.get(key) or 0) for key in ("tokens", "calls", "cache_hit")}}
            except (TypeError, ValueError):
                pass
        for name in ("last_tag_call", "last_deep_call"):
            value = data.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(self, f"_{name}", float(value))
        deep = data.get("last_deep")
        if isinstance(deep, dict):
            try:
                self.last_deep = DeepRead(**deep)
            except TypeError:
                self.last_deep = None

    def _save(self):
        data = {
            "version": 1,
            "tags": self._tags,
            "usage": self._usage,
            "last_tag_call": self._last_tag_call,
            "last_deep_call": self._last_deep_call,
            "last_deep": asdict(self.last_deep) if self.last_deep else None,
        }
        directory = os.path.dirname(os.path.abspath(self._cache_path))
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory, prefix=".goldmonitor-ai-", suffix=".tmp", delete=False,
            ) as f:
                temporary = f.name
                json.dump(data, f, ensure_ascii=False)
            os.replace(temporary, self._cache_path)
            temporary = None
        except (OSError, TypeError, ValueError):
            pass
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
