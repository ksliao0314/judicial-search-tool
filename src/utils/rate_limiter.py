"""Async token-bucket rate limiter，用於 Claude API 的 ITPM / RPM 控制。

Anthropic Tier 1 限制（sonnet-4-6）：
  - Input tokens per minute (ITPM): 30,000
  - Output tokens per minute (OTPM): 較寬鬆（我們 max_tokens=512，用量極小）
  - Requests per minute (RPM): 50

以「input token」為主要約束：每次呼叫前估算 input tokens 並 acquire，
bucket 無足夠 token 時自動 sleep 到補充夠用為止。這比固定 concurrency
更 robust，尤其當某些判決文特別長時能主動退讓。
"""
from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Async token bucket。

    rate_per_minute: 每分鐘補充的 token 數（同時也是預設 capacity）。
    capacity: 桶的最大容量；允許短時 burst。預設 = rate_per_minute。
    """

    def __init__(self, rate_per_minute: int, capacity: int | None = None) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be > 0")
        self._rate_per_sec = rate_per_minute / 60.0
        self._capacity = float(capacity if capacity is not None else rate_per_minute)
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0) -> None:
        """請求 n 個 token；不夠就 sleep 到夠為止。"""
        if n > self._capacity:
            raise ValueError(f"requested {n} > capacity {self._capacity}")

        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_sec)
                self._last_refill = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                wait = deficit / self._rate_per_sec
            # lock 釋放後再 sleep，其他 coroutine 不會被卡住
            await asyncio.sleep(wait)

    @property
    def available(self) -> float:
        """近似目前可用 tokens（無 lock 的快照，僅供 debug / log）。"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        return min(self._capacity, self._tokens + elapsed * self._rate_per_sec)


def estimate_prompt_tokens(text: str) -> int:
    """粗略估算繁體中文混英文 prompt 的 input token 數。

    Anthropic tokenizer 對中文通常 1 token ≈ 1 CJK 字，英文 1 token ≈ 4 chars。
    我們 prompt 以中文為主，保守估算：1 char → 1.0 token（含 ASCII 偏差緩衝）。
    這個上估比低估好 — 讓 bucket 更保守。
    """
    return len(text)
