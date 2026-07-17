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
    # （實作見下方；模組尾端另有對司法院外連的全域共用 bucket）

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
    """估算繁體中文混英文 prompt 的 input token 數。

    舊版用 `len(text)`（1 char ≈ 1 token），對中文嚴重高估：Anthropic tokenizer 對
    繁中常 1 token ≈ 1.5-2 字、ASCII 1 token ≈ 4 chars。1.0/char 會讓 bucket 被多扣
    ~2 倍、把本可並行的呼叫白白序列化（吞吐受限）。

    改為分字類保守估算：
      - 非 ASCII（CJK 為主）: 0.6 token/char  — 比真實 (~0.5) 高一點留安全邊際
      - ASCII（英數標點）:     0.3 token/char  — 4 char/token + 緩衝
    仍刻意「寧可上估」（避免低估觸發 429、429 retry 要等 30s 滾動視窗），但把過度高估
    從 ~2 倍收斂到 ~1.2 倍，釋放吞吐。最終 retry/429 路徑仍是安全網。
    """
    if not text:
        return 0
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    cjk_n = len(text) - ascii_n
    return int(cjk_n * 0.6 + ascii_n * 0.3) + 8  # +8: 訊息框架 overhead 緩衝


# ---------------------------------------------------------------------------
# 全域共用：app 對司法院（judgment / cons.judicial.gov.tw）的所有外連預算
# ---------------------------------------------------------------------------
# 為什麼要「所有路徑共用一個」而不是各路徑各開一個 bucket：
#   F5 WAF 看的是「同一個來源 IP 的總請求速率」，不是各程式路徑各自的速率。各開各的
#   會讓總量疊加（get_judgment 60/min + PDF 直抓 30/min + …），而 app 這邊沒有任何
#   地方在管總和。
#
#   2026-07-16 實地事故：PDF 直抓那條路徑當時「只限並發(Semaphore 2)、沒限速率」，
#   41 筆批次疊上重試風暴衝到 120~360 req/min，把使用者「事務所的對外 IP」整個打到被
#   F5 網路層封鎖（TLS 通、一送 HTTP 就 RST），全所同事一起連不上司法院數小時。
#
#   統一成單一預算後，app 端對司法院的總速率「可證明」不超過 60/min —— 新增任何抓取
#   功能都只會分食這個額度，不會再把總量往上疊。
#   （MCP subprocess 的 search 有自己的 1-2s 間隔節流，屬不同 process、無法共用此桶。）
judicial_bucket = TokenBucket(rate_per_minute=60, capacity=30)


# 全域共用：app 對「勞動部訴願」(appealweb.mol.gov.tw) 的所有外連預算。
# 與司法院分開計（不同主機各自算自己的來源 IP 速率），但道理相同：
#   Stage 2.5 以 FETCH_CONCURRENCY=5 併發抓訴願詳情頁，原本完全沒限速
#   （filter._fetch_one 的 appeal 分支註解還明寫「不經 bucket」），分析數百筆時
#   可輕易衝上數百 req/min —— 與 2026-07-16 把事務所 IP 打到被司法院封鎖的
#   完全同一個 bug 型態，只是換一個站台。預防性補上。
# appealweb 是小型政府站台，且我們的自動化會辨識其驗證碼 → 更該保守禮貌。
appeal_bucket = TokenBucket(rate_per_minute=60, capacity=20)
